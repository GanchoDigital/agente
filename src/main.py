from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
import os
from openai import OpenAI
from typing import Optional, Dict, Any
import logging
import base64
from datetime import datetime, timedelta
from supabase import create_client
import asyncio
import tempfile
import re
import traceback
import json
import sys

# Configuração de logging com UTF-8
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    encoding='utf-8',  # Forçar UTF-8
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Carrega variáveis de ambiente
try:
    load_dotenv()
except Exception as e:
    logger.warning(f"Não foi possível carregar o arquivo .env: {str(e)}")

# Configurações
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "https://evo.ganchodigital.com.br")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Logs para debug
logger.info(f"EVOLUTION_API_URL: {EVOLUTION_API_URL}")
logger.info(f"OPENAI_API_KEY definida: {bool(OPENAI_API_KEY)}")
logger.info(f"EVOLUTION_API_KEY definida: {bool(EVOLUTION_API_KEY)}")
logger.info(f"SUPABASE_URL definida: {bool(SUPABASE_URL)}")
logger.info(f"SUPABASE_KEY definida: {bool(SUPABASE_KEY)}")

# Valida as credenciais
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY não definida no ambiente")
if not EVOLUTION_API_KEY:
    logger.error("EVOLUTION_API_KEY não definida no ambiente")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("SUPABASE_URL ou SUPABASE_KEY não definidas no ambiente")

app = FastAPI(title="WhatsApp GPT Bot")
openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Dicionário para armazenar mensagens pendentes
pending_messages = {}
pending_tasks = {}

class MessageContextInfo(BaseModel):
    deviceListMetadata: Dict[str, Any] = None
    deviceListMetadataVersion: int = None
    messageSecret: str = None

class ImageMessage(BaseModel):
    url: str
    mimetype: str
    fileSha256: str
    fileLength: str
    height: int
    width: int
    mediaKey: str
    fileEncSha256: str
    directPath: str
    mediaKeyTimestamp: str
    jpegThumbnail: Optional[str]
    scansSidecar: Optional[str]
    scanLengths: Optional[list]
    midQualityFileSha256: Optional[str]

class Message(BaseModel):
    conversation: Optional[str] = None
    messageContextInfo: Optional[MessageContextInfo] = None
    imageMessage: Optional[ImageMessage] = None
    base64: Optional[str] = None  # Para mensagens de áudio

class MessageKey(BaseModel):
    remoteJid: str
    fromMe: bool
    id: str

class MessageData(BaseModel):
    key: MessageKey
    pushName: str
    status: str
    message: Message
    messageType: str
    messageTimestamp: int
    instanceId: str
    source: str

class WhatsAppWebhook(BaseModel):
    event: str
    instance: str
    data: MessageData
    destination: str
    date_time: str
    sender: str
    server_url: str
    apikey: str

async def check_and_create_contact(phone: str, instance_name: str, push_name: str, from_me: bool) -> Optional[Dict]:
    try:
        # Limpa o número do telefone (remove @s.whatsapp.net)
        phone = phone.split('@')[0]
        
        # Busca o contato
        response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('instance_name', instance_name).execute()
        
        if not response.data:
            # Se não existe, cria um novo contato
            thread = openai_client.beta.threads.create()
            new_contact = {
                'name': push_name or f'User {phone}',
                'whatsapp': phone,
                'status': 'ativo',
                'instance_name': instance_name,
                'last_contact': datetime.utcnow().isoformat(),
                'thread_id': thread.id,
                'followup': False,
                'etapa': 'conexão',
                'from_me': from_me
            }
            response = supabase.table('contacts').insert(new_contact).execute()
            logger.info(f"Novo contato criado: {phone}")
            return response.data[0]
        
        contact = response.data[0]
        
        if from_me:
            cooldown_end = (datetime.utcnow() + timedelta(hours=24)).isoformat()
            supabase.table('contacts').update({
                'last_contact': datetime.utcnow().isoformat(),
                'from_me': True,
                'status': 'cooldown',
                'cooldown_until': cooldown_end
            }).eq('whatsapp', phone).eq('instance_name', instance_name).execute()
            logger.info(f"Contato {phone} entrou em cooldown")
        else:
            supabase.table('contacts').update({
                'last_contact': datetime.utcnow().isoformat(),
                'name': push_name or contact['name']
            }).eq('whatsapp', phone).eq('instance_name', instance_name).execute()
            logger.info(f"Contato {phone} atualizado")
        
        return contact

    except Exception as e:
        logger.error(f"Erro ao verificar/criar contato: {str(e)}")
        raise

async def process_image(image_base64: str) -> str:
    try:
        # Analisa a imagem usando gpt-4o (que tem capacidade de visão)
        response = openai_client.chat.completions.create(
            model="gpt-4o",  # Use gpt-4o que tem visão, não gpt-4o-mini
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Descreva detalhadamente esta imagem em português."
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=500
        )
        
        return response.choices[0].message.content

    except Exception as e:
        logger.error(f"Erro ao processar imagem: {str(e)}")
        return "[Não foi possível processar a imagem]"

async def process_audio(audio_base64: str) -> str:
    try:
        # Decodifica o áudio base64
        audio_bytes = base64.b64decode(audio_base64)
        
        # Cria um arquivo temporário para o áudio
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as temp_audio:
            temp_audio.write(audio_bytes)
            temp_audio_path = temp_audio.name

        # Transcreve o áudio usando OpenAI
        with open(temp_audio_path, 'rb') as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )

        # Limpa o arquivo temporário
        os.unlink(temp_audio_path)

        return transcript.text

    except Exception as e:
        logger.error(f"Erro ao processar áudio: {str(e)}")
        return "[Não foi possível transcrever o áudio]"

async def send_webhook_request(function_name: str, function_args: dict) -> bool:
    """Envia requisição para o webhook com o nome da função."""
    try:
        webhook_base_url = "https://webhook.ganchodigital.com.br/webhook"
        webhook_url = f"{webhook_base_url}/{function_name}"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                webhook_url,
                json=function_args,
                timeout=30
            )
            
        logger.info(f"Webhook chamado para função {function_name}: Status {response.status_code}")
        return response.status_code in [200, 201, 202]
    except Exception as e:
        logger.error(f"Erro ao chamar webhook para função {function_name}: {str(e)}")
        return False

async def check_contact_limit(instance_name: str, contact_number: str) -> bool:
    """
    Verifica se o usuário atingiu o limite de contatos do seu plano.
    Retorna True se ainda está dentro do limite, False se excedeu.
    """
    try:
        # Busca o usuário responsável pelo assistente
        logger.info(f"Verificando limite para assistente: {instance_name}")
        assistant_data = supabase.table('assistants').select(
            'user_id'
        ).eq('instance_name', instance_name).execute()
        
        if not assistant_data.data or len(assistant_data.data) == 0:
            logger.error(f"Assistente não encontrado: {instance_name}")
            return True  # Permite continuar se não encontrar o assistente
        
        user_id = assistant_data.data[0]['user_id']
        logger.info(f"ID do usuário encontrado: {user_id}")
        
        # Tenta obter a estrutura da tabela users primeiro
        try:
            # Lista todas as colunas da tabela users
            logger.info("Verificando o nome correto da coluna na tabela users...")
            
            # Tentativa com uma abordagem diferente - consultar todos os campos
            user_data = supabase.table('users').select('*').eq('uuid', user_id).execute()
            if user_data.data and len(user_data.data) > 0:
                logger.info(f"Colunas disponíveis na tabela users: {list(user_data.data[0].keys())}")
                user_plan = user_data.data[0].get('plan')
                if not user_plan:
                    logger.warning(f"Campo 'plan' não encontrado no usuário. Dados disponíveis: {user_data.data[0]}")
                    user_plan = 'starter'  # Plano padrão
            else:
                # Tenta com diferentes nomes possíveis de coluna
                for possible_id_column in ['uuid', 'user_id', 'id', 'ID']:
                    try:
                        logger.info(f"Tentando usar coluna '{possible_id_column}' na tabela users")
                        user_data = supabase.table('users').select('*').eq(possible_id_column, user_id).execute()
                        if user_data.data and len(user_data.data) > 0:
                            logger.info(f"Coluna '{possible_id_column}' funciona! Colunas disponíveis: {list(user_data.data[0].keys())}")
                            user_plan = user_data.data[0].get('plan')
                            if not user_plan:
                                logger.warning(f"Campo 'plan' não encontrado. Dados disponíveis: {user_data.data[0]}")
                                user_plan = 'starter'  # Plano padrão
                            break
                    except Exception as e:
                        logger.warning(f"Falha ao tentar coluna '{possible_id_column}': {str(e)}")
                else:
                    # Se nenhuma coluna funcionar
                    logger.error(f"Não foi possível encontrar o usuário com nenhum campo de ID. Usando plano 'starter'")
                    user_plan = 'starter'  # Plano padrão
        except Exception as e:
            logger.error(f"Erro ao tentar verificar estrutura da tabela: {str(e)}")
            # Usa plano starter como fallback
            user_plan = 'starter'
        
        # Define limite baseado no plano
        plan_limits = {
            'starter': 100,
            'essential': 500,
            'agent': 2000,
            'empresa': 10000
        }
        
        # Se user_plan estiver definido como None, use o valor padrão
        if not user_plan:
            user_plan = 'starter'
            
        contact_limit = plan_limits.get(user_plan.lower() if isinstance(user_plan, str) else 'starter', 100)
        
        # Calcula data de 30 dias atrás
        thirty_days_ago = datetime.now() - timedelta(days=30)
        
        # Conta contatos do usuário nos últimos 30 dias
        try:
            # Tenta usar a coluna user_id que o assistente preencheu
            contacts_data = supabase.table('contacts').select(
                'id'
            ).eq('user_id', user_id).gte(
                'created_at', thirty_days_ago.isoformat()
            ).execute()
            
            current_contacts = len(contacts_data.data) if contacts_data.data else 0
            logger.info(f"Contatos nos últimos 30 dias: {current_contacts}")
        except Exception as e:
            logger.error(f"Erro ao contar contatos: {str(e)}")
            return True  # Em caso de erro na contagem, permite continuar
        
        logger.info("Verificação de limite: " +
            f"Usuário: {user_id}, " +
            f"Plano: {user_plan}, " +
            f"Limite: {contact_limit}, " +
            f"Contatos atuais: {current_contacts}"
        )
        
        return current_contacts < contact_limit
        
    except Exception as e:
        logger.error(f"Erro crítico ao verificar limite de contatos: {str(e)}")
        # Em caso de erro, permite prosseguir
        return True

async def send_whatsapp_messages(response_text: str, phone: str, instance_name: str, apikey: str, server_url: str):
    """Divide a mensagem do assistente em partes e envia como mensagens separadas."""
    try:
        # Substituir asteriscos duplos por simples
        # Converte "**texto**" para "*texto*"
        response_text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', response_text)
        
        # Se a mensagem completa for curta (menos de 100 caracteres), envia como uma única mensagem
        if len(response_text.strip()) < 100:
            logger.info(f"Mensagem curta ({len(response_text.strip())} caracteres), enviando sem dividir")
            
            evolution_url = f"{server_url}/message/sendText/{instance_name}"
            
            headers = {
                "Content-Type": "application/json",
                "apikey": apikey
            }
            
            payload = {
                "number": phone,
                "text": response_text.strip(),
                "delay": 1200
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    evolution_url,
                    headers=headers,
                    json=payload
                )
                
                logger.info(f"Resposta da requisição: Status {response.status_code}")
                if response.status_code != 200 and response.status_code != 201:
                    logger.error(f"Erro ao enviar mensagem: {response.text}")
                    
            return True
        
        # NOVA ABORDAGEM: identificar e agrupar por tópicos numerados
        
        # Dividir o texto por tópicos numerados
        # Regex para identificar um tópico numerado (ex: "1. Texto" ou "1. Texto\nOutro texto")
        # Cada tópico termina quando começa o próximo número ou acaba o texto
        topic_pattern = r'(\d+\.\s*[^\n\d]*(?:\n[^\n\d]+)*)'
        
        # Encontrar todos os tópicos numerados
        topics = re.findall(topic_pattern, response_text)
        
        # Verificar se temos tópicos ou se precisamos de outra abordagem
        if topics and len(topics) > 1:
            logger.info(f"Encontrados {len(topics)} tópicos numerados para envio")
            
            # Extrair cabeçalho (texto antes do primeiro tópico numerado)
            header_match = re.match(r'(.*?)(?=\d+\.)', response_text, re.DOTALL)
            header = header_match.group(1).strip() if header_match else None
            
            # Separar partes da mensagem
            parts = []
            
            # Adicionar cabeçalho se existir
            if header and len(header) > 0:
                parts.append(header)
            
            # Agrupar tópicos que são curtos
            current_group = ""
            for topic in topics:
                # Se o tópico for muito longo (mais de 250 caracteres), enviar separado
                if len(topic) > 250:
                    # Se já temos um grupo, envia primeiro
                    if current_group:
                        parts.append(current_group.strip())
                        current_group = ""
                    
                    # Envia o tópico longo separadamente
                    parts.append(topic.strip())
                    continue
                
                # Se tópico é curto e adicionar ao grupo atual mantém tamanho gerenciável
                if len(current_group) + len(topic) < 300:
                    # Adiciona uma quebra de linha simples entre os tópicos
                    if current_group:
                        current_group += "\n" + topic  # Modificado para usar \n em vez de concatenar diretamente
                    else:
                        current_group = topic
                else:
                    # O grupo ficaria muito grande, então finalizamos o atual
                    if current_group:
                        parts.append(current_group.strip())
                    current_group = topic
            
            # Adicionar o último grupo se houver
            if current_group:
                parts.append(current_group.strip())
        else:
            # Não encontramos tópicos numerados bem definidos
            # Dividir por parágrafos ou blocos lógicos
            paragraphs = response_text.split('\n\n')
            
            # Se temos parágrafos bem definidos
            if len(paragraphs) > 1:
                parts = []
                current_group = ""
                
                for paragraph in paragraphs:
                    # Verifica se o parágrafo contém um tópico numerado
                    contains_topic = bool(re.search(r'^\d+\.', paragraph.strip()))
                    
                    # Se for um tópico numerado e for curto, agrupa com o próximo
                    if contains_topic and len(paragraph) < 50:
                        # Se já temos um grupo, verificamos se o próximo tópico deve ser junto
                        if current_group and not re.search(r'^\d+\.', current_group.strip()):
                            parts.append(current_group.strip())
                            current_group = paragraph
                        else:
                            # Continua agrupando
                            if current_group:
                                current_group += "\n" + paragraph  # Modificado para usar \n em vez de \n\n
                            else:
                                current_group = paragraph
                    else:
                        # Se não for tópico ou for longo, envia separado
                        if current_group:
                            parts.append(current_group.strip())
                        current_group = paragraph
                
                # Adicionar o último grupo
                if current_group:
                    parts.append(current_group.strip())
            else:
                # Texto sem estrutura clara de parágrafos ou tópicos
                # Dividir por frases (pontuação)
                sentences = re.split(r'(?<=[.!?])\s+', response_text)
                
                parts = []
                current_part = ""
                for sentence in sentences:
                    # Se a adição desta frase mantiver o tamanho abaixo de 200 caracteres
                    if len(current_part + sentence) < 200:
                        current_part += sentence + " "
                    else:
                        # A parte ficaria muito grande, finalizamos a atual
                        if current_part.strip():
                            parts.append(current_part.strip())
                        current_part = sentence + " "
                
                # Adicionar a última parte
                if current_part.strip():
                    parts.append(current_part.strip())
        
        # Formatar as partes finais
        formatted_parts = []
        for part in parts:
            # Garantir que o texto tenha formatação markdown correta
            # Converter **texto** para *texto* se ainda houver algum
            formatted = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', part.strip())
            # Corrigir números de lista que não têm espaço após o ponto
            formatted = re.sub(r'(\d+\.)([^\s])', r'\1 \2', formatted)
            formatted_parts.append(formatted)
        
        # Filtrar partes vazias
        parts = [p for p in formatted_parts if p.strip()]
        
        # Log das partes para debug
        logger.info(f"Mensagem dividida em {len(parts)} partes:")
        for i, part in enumerate(parts):
            logger.info(f"Parte {i+1}: {part}")
        
        # Enviar cada parte como uma mensagem separada
        logger.info("Iniciando envio de mensagens...")
        async with httpx.AsyncClient() as client:
            for i, message in enumerate(parts):
                logger.info(f"Enviando parte {i+1}/{len(parts)}: {message}")
                
                evolution_url = f"{server_url}/message/sendText/{instance_name}"
                
                headers = {
                    "Content-Type": "application/json",
                    "apikey": apikey
                }
                
                payload = {
                    "number": phone,
                    "text": message,
                    "delay": 1200
                }
                
                response = await client.post(
                    evolution_url,
                    headers=headers,
                    json=payload
                )
                
                logger.info(f"Resposta da requisição {i+1}: Status {response.status_code}")
                if response.status_code != 200 and response.status_code != 201:
                    logger.error(f"Erro ao enviar mensagem: {response.text}")
                
                # Aguardar um tempo entre o envio de cada mensagem
                await asyncio.sleep(1)
            
        logger.info("Todas as mensagens foram enviadas com sucesso")
        return True
    except Exception as e:
        logger.error(f"Erro ao dividir e enviar mensagens: {str(e)}")
        logger.error(traceback.format_exc())
        return False

async def send_notification(
    target_number: str,
    client_number: str,
    client_name: str,
    context: str,
    instance_name: str,
    apikey: str,
    server_url: str
):
    """Envia notificação para um número específico sobre um cliente aguardando."""
    try:
        notification_message = (
            "Um cliente está aguardando o seu contato\n\n"
            f"Nome: {client_name}\n"
            f"Número: {client_number}\n"
            f"Contexto: {context}"
        )

        evolution_url = f"{server_url}/message/sendText/{instance_name}"
        
        headers = {
            "Content-Type": "application/json",
            "apikey": apikey
        }
        
        # Simplificando o payload para usar o formato que funciona
        payload = {
            "number": target_number,
            "text": notification_message,
            "delay": 1200
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                evolution_url,
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200:
                logger.error(f"Erro ao enviar notificação: {response.text}")
            
        logger.info(f"Notificação enviada para {target_number}")
        return response.status_code == 200

    except Exception as e:
        logger.error(f"Erro ao enviar notificação: {str(e)}")
        return False

async def update_contact_stage(phone: str, stage: str, instance_name: str) -> bool:
    """Atualiza o estágio do contato no banco de dados."""
    try:
        data = supabase.table('contacts').update(
            {"etapa": stage}
        ).eq('whatsapp', phone).execute()
        
        logger.info(f"Estágio do contato {phone} atualizado para: {stage}")
        
        # Adicionando log para verificar a resposta do Supabase
        logger.info(f"Resposta do Supabase: {data}")
        
        return True
    except Exception as e:
        logger.error(f"Erro ao atualizar estágio do contato: {str(e)}")
        # Adicionando log detalhado do erro
        logger.error(f"Detalhes do erro: phone={phone}, stage={stage}")
        return False

async def send_image(phone: str, instance_name: str, apikey: str, server_url: str, image_id: str) -> bool:
    """Envia uma imagem para o usuário usando o ID da imagem do banco de dados."""
    try:
        # Busca a imagem no banco de dados
        response = supabase.table('imagens').select('link').eq('image_id', image_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Imagem não encontrada com ID: {image_id}")
            return False
            
        image_link = response.data[0]['link']
        logger.info(f"Link da imagem encontrado: {image_link}")
        
        # Prepara a requisição para a Evolution API
        evolution_url = f"{server_url}/message/sendMedia/{instance_name}"
        
        headers = {
            "Content-Type": "application/json",
            "apikey": apikey
        }
        
        payload = {
            "number": phone,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "media": image_link,
            "fileName": "imagem.jpg",
            "delay": 1200
        }
        
        # Envia a requisição
        async with httpx.AsyncClient() as client:
            response = await client.post(
                evolution_url,
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200 and response.status_code != 201:
                logger.error(f"Erro ao enviar imagem: {response.text}")
                return False
                
        logger.info(f"Imagem enviada com sucesso para {phone}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao enviar imagem: {str(e)}")
        return False

async def process_delayed_message(phone: str, instance_name: str, apikey: str, server_url: str):
    # Define a chave única no início da função
    key = f"{phone}:{instance_name}"
    
    try:
        # Espera 5 segundos
        await asyncio.sleep(5)
        
        # Verifica se ainda existem mensagens pendentes para este contato
        if key not in pending_messages:
            logger.info(f"Nenhuma mensagem pendente encontrada para {key}")
            return
            
        # Obtém todas as mensagens acumuladas
        messages = pending_messages.pop(key, [])
        if not messages:
            return
            
        # Concatena as mensagens
        concatenated_message = " ".join([msg for msg in messages])
        logger.info(f"Processando {len(messages)} mensagens concatenadas para {phone}")
        
        # Processa a mensagem concatenada
        contact = await check_and_create_contact(phone, instance_name, "", False)
        
        if not contact or contact['status'] != 'ativo':
            logger.info(f"Contato {phone} não está ativo")
            return
            
        # Usa a thread existente do contato
        thread_id = contact['thread_id']
        
        # Antes de criar uma nova mensagem, verifica e cancela runs ativas
        try:
            runs = openai_client.beta.threads.runs.list(thread_id=thread_id)
            for run in runs.data:
                if run.status in ['in_progress', 'queued', 'requires_action']:
                    logger.info(f"Cancelando run ativa: {run.id}")
                    openai_client.beta.threads.runs.cancel(
                        thread_id=thread_id,
                        run_id=run.id
                    )
                    await asyncio.sleep(1)  # Pequena pausa para garantir que o cancelamento foi processado
        except Exception as e:
            logger.error(f"Erro ao cancelar run: {str(e)}")

        # Agora podemos adicionar a nova mensagem com segurança
        thread_message = openai_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=concatenated_message
        )
        logger.info(f"Mensagens concatenadas adicionadas à thread: {thread_message.id}")
        
        # Executa o assistente
        run = openai_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=instance_name
        )
        
        # Adiciona timeout e tratamento de estados de erro
        max_retries = 30
        retry_count = 0
        
        while True:
            run = openai_client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run.id
            )
            
            if run.status == 'requires_action' and run.required_action.type == 'submit_tool_outputs':
                tool_outputs = []
                
                for tool_call in run.required_action.submit_tool_outputs.tool_calls:
                    function_name = tool_call.function.name
                    logger.info(f"Processando função: {function_name}")
                    
                    try:
                        function_args = json.loads(tool_call.function.arguments)
                        
                        if function_name == 'notificar':
                            # Obtém informações do contato
                            contact = await check_and_create_contact(phone, instance_name, "", False)
                            client_name = contact.get('name', 'Nome não disponível')
                            
                            # Obtém o contexto da conversa (últimas mensagens)
                            messages = openai_client.beta.threads.messages.list(thread_id=thread_id)
                            context = messages.data[1].content[0].text.value if len(messages.data) > 1 else "Sem contexto disponível"
                            
                            # Envia a notificação
                            success = await send_notification(
                                target_number=function_args.get('numero'),
                                client_number=phone,
                                client_name=client_name,
                                context=context,
                                instance_name=instance_name,
                                apikey=apikey,
                                server_url=server_url
                            )
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({"success": success})
                            })
                            
                        elif function_name == 'funil_de_vendas':
                            stage = function_args.get('estagio')
                            
                            # Atualiza o estágio do contato
                            success = await update_contact_stage(phone, stage, instance_name)
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": f"Contato atualizado para estágio: {stage}" if success else "Falha ao atualizar estágio"
                                })
                            })
                            
                            logger.info(f"Função funil_de_vendas processada para {phone}: {stage}")
                            
                        elif function_name == 'enviar_imagem':
                            image_id = function_args.get('imagem_id')
                            
                            # Envia a imagem
                            success = await send_image(
                                phone=phone,
                                instance_name=instance_name,
                                apikey=apikey,
                                server_url=server_url,
                                image_id=image_id
                            )
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": f"Imagem enviada com sucesso" if success else "Falha ao enviar imagem"
                                })
                            })
                            
                            logger.info(f"Função enviar_imagem processada para {phone}: {image_id}")
                            
                        else:
                            # Para qualquer outra função, envia para o webhook
                            logger.info(f"Enviando função {function_name} para webhook")
                            success = await send_webhook_request(function_name, function_args)
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": f"Webhook chamado para função: {function_name}"
                                })
                            })
                            
                    except Exception as e:
                        logger.error(f"Erro ao processar função {function_name}: {str(e)}")
                        tool_outputs.append({
                            "tool_call_id": tool_call.id,
                            "output": json.dumps({"success": False, "error": str(e)})
                        })
                
                # Submete todas as respostas das funções
                if tool_outputs:
                    logger.info(f"Submetendo resultados das funções: {tool_outputs}")
                    run = openai_client.beta.threads.runs.submit_tool_outputs(
                        thread_id=thread_id,
                        run_id=run.id,
                        tool_outputs=tool_outputs
                    )
            
            # Se concluído com sucesso, sai do loop
            if run.status == 'completed':
                break
                
            # Se falhou, cancelado ou expirou, gera mensagem de erro
            if run.status in ['failed', 'cancelled', 'expired']:
                logger.error(f"Assistente falhou: {run.status}")
                return
            
            # Limita o número de tentativas
            retry_count += 1
            if retry_count >= max_retries:
                logger.error("Timeout ao processar mensagem")
                return
            
            # Aguarda antes de verificar novamente
            await asyncio.sleep(2)
        
        messages = openai_client.beta.threads.messages.list(thread_id=thread_id)
        response_text = messages.data[0].content[0].text.value
        logger.info(f"Resposta completa do assistente: {response_text[:200]}...")
        
        # Agora vamos dividir e enviar as mensagens
        logger.info("Iniciando divisão e envio de mensagens...")
        success = await send_whatsapp_messages(
            response_text=response_text,
            phone=phone,
            instance_name=instance_name,
            apikey=apikey,
            server_url=server_url
        )
        
        if success:
            logger.info("Todas as mensagens foram enviadas com sucesso")
        else:
            logger.error("Falha ao enviar mensagens")
            
    except asyncio.CancelledError:
        logger.info(f"Tarefa cancelada para {key}")
        # Limpa a tarefa dos pendentes se existir
        if key in pending_tasks:
            del pending_tasks[key]
        raise  # Re-levanta a exceção para proper cleanup
            
    except Exception as e:
        logger.error(f"Erro ao processar mensagem com delay: {str(e)}")
        traceback.print_exc()  # Adiciona rastreamento completo do erro
    finally:
        # Limpa a tarefa pendente em qualquer caso
        if key in pending_tasks:
            del pending_tasks[key]

@app.post("/webhook")
async def webhook(data: WhatsAppWebhook):
    try:
        # Verifica se é um evento de mensagem
        if data.event != "messages.upsert":
            return {"success": True, "message": "Evento ignorado"}

        # Extrai informações relevantes
        phone = data.data.key.remoteJid.split('@')[0]
        instance_name = data.instance
        push_name = data.data.pushName
        from_me = data.data.key.fromMe
        message_type = data.data.messageType
        
        if from_me:
            await check_and_create_contact(phone, instance_name, push_name, from_me)
            logger.info(f"Mensagem descartada para {phone}. Motivo: Mensagem do usuário")
            return {"success": False, "message": "Mensagem do usuário"}

        # Verifica limite de contatos
        within_limit = await check_contact_limit(instance_name, phone)
        
        if not within_limit:
            # Se excedeu o limite, envia mensagem informando
            message = (
                "Desculpe, o limite de contatos do plano atual foi atingido. "
                "Por favor, entre em contato com o suporte para upgrade do plano."
            )
            
            evolution_url = f"{data.server_url}/message/sendText/{instance_name}"
            
            headers = {
                "Content-Type": "application/json",
                "apikey": data.apikey
            }
            
            # Simplificando o payload para usar o formato que funciona
            payload = {
                "number": phone,
                "text": message,
                "delay": 1200
            }
            
            async with httpx.AsyncClient() as client:
                await client.post(
                    evolution_url,
                    headers=headers,
                    json=payload
                )
            
            logger.warning(f"Limite de contatos excedido para instância {instance_name}")
            return {"status": "error", "message": "Contact limit exceeded"}

        contact = await check_and_create_contact(phone, instance_name, push_name, from_me)
        
        if not contact or contact['status'] != 'ativo':
            logger.info(f"Mensagem descartada para {phone}. Status: {contact['status'] if contact else 'não encontrado'}")
            return {"success": False, "message": "Contato não está ativo"}

        # Processa diferentes tipos de mensagem
        user_message = ""
        
        if message_type == "conversation":
            user_message = data.data.message.conversation
        elif message_type == "imageMessage":
            try:
                # Usa o jpegThumbnail que já vem em base64
                image_base64 = data.data.message.imageMessage.jpegThumbnail
                
                # Se o thumbnail estiver disponível, processa a imagem
                if image_base64:
                    logger.info("Processando imagem com GPT-4o")
                    # Obtem descrição da imagem
                    response = openai_client.chat.completions.create(
                        model="gpt-4o",
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Descreva detalhadamente esta imagem em português."
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/jpeg;base64,{image_base64}"
                                        }
                                    }
                                ]
                            }
                        ],
                        max_tokens=500
                    )
                    image_description = response.choices[0].message.content
                    logger.info(f"Descrição da imagem gerada: {image_description[:100]}...")
                    # Envia a descrição como contexto para o assistente
                    user_message = f"O usuário enviou uma imagem. Descrição da imagem: {image_description}"
                    logger.info("Enviando descrição para o assistente como contexto")
                else:
                    user_message = "O usuário enviou uma imagem que não foi possível processar."
                    logger.info("Imagem sem thumbnail disponível")
            except Exception as e:
                logger.error(f"Erro ao processar imagem: {str(e)}")
                user_message = "O usuário enviou uma imagem que não foi possível processar."
        elif message_type == "audioMessage":
            try:
                # Obtém o áudio diretamente do campo base64
                if hasattr(data.data.message, "base64") and data.data.message.base64:
                    audio_base64 = data.data.message.base64
                    
                    # Transcreve o áudio
                    transcription = await process_audio(audio_base64)
                    logger.info(f"Transcrição do áudio gerada: {transcription[:100]}...")
                    
                    # Envia a transcrição como contexto para o assistente
                    user_message = f"O usuário enviou um áudio. Transcrição do áudio: {transcription}"
                else:
                    user_message = "O usuário enviou um áudio que não foi possível transcrever."
                    logger.info("Áudio sem dados base64 disponíveis")
            except Exception as e:
                logger.error(f"Erro ao processar áudio: {str(e)}")
                user_message = "O usuário enviou um áudio que não foi possível transcrever."
        else:
            logger.warning(f"Tipo de mensagem não suportado: {message_type}")
            return {"success": False, "message": "Tipo de mensagem não suportado"}

        if not user_message:
            return {"success": False, "message": "Mensagem vazia"}

        # Adiciona a mensagem à lista de mensagens pendentes
        key = f"{phone}:{instance_name}"
        if key not in pending_messages:
            pending_messages[key] = []
        
        pending_messages[key].append(user_message)
        logger.info(f"Mensagem adicionada à fila para {phone}. Total: {len(pending_messages[key])}")
        
        # Se já existe uma tarefa pendente para este contato, não cria outra
        if key in pending_tasks and not pending_tasks[key].done():
            logger.info(f"Já existe uma tarefa pendente para {key}")
            return {"success": True, "message": "Mensagem adicionada à fila existente"}
            
        # Cria uma nova tarefa para processar após 5 segundos
        task = asyncio.create_task(
            process_delayed_message(phone, instance_name, data.apikey, data.server_url)
        )
        pending_tasks[key] = task
        logger.info(f"Nova tarefa de processamento criada para {key}")

        return {"success": True, "message": "Mensagem adicionada à fila"}

    except Exception as e:
        logger.error(f"Erro no processamento: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "evolution_api": EVOLUTION_API_URL,
        "openai": "configured"
    }

async def download_image(url: str, headers: Dict) -> str:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                # Converte a imagem para base64
                return base64.b64encode(response.content).decode('utf-8')
            else:
                raise HTTPException(status_code=response.status_code, detail="Erro ao baixar imagem")
    except Exception as e:
        logger.error(f"Erro ao baixar imagem: {str(e)}")
        raise

async def download_audio(url: str, headers: Dict) -> str:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                # Converte o áudio para base64
                return base64.b64encode(response.content).decode('utf-8')
            else:
                raise HTTPException(status_code=response.status_code, detail="Erro ao baixar áudio")
    except Exception as e:
        logger.error(f"Erro ao baixar áudio: {str(e)}")
        raise

if __name__ == "__main__":
    import uvicorn
    logger.info("Iniciando servidor...")
    try:
        # Verifica se todas as dependências necessárias estão presentes
        logger.info("Verificando dependências necessárias...")
        
        # Tenta criar um cliente do Supabase
        try:
            supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
            logger.info("Conexão com Supabase estabelecida com sucesso")
        except Exception as e:
            logger.error(f"⚠️ ERRO DE INICIALIZAÇÃO: Falha ao conectar ao Supabase: {str(e)}")
            
        # Tenta criar um cliente OpenAI
        try:
            openai_test = OpenAI(api_key=OPENAI_API_KEY)
            logger.info("Cliente OpenAI inicializado com sucesso")
        except Exception as e:
            logger.error(f"⚠️ ERRO DE INICIALIZAÇÃO: Falha ao inicializar OpenAI: {str(e)}")
        
        # Inicia o servidor com desativação de recarregamento automático
        logger.info("Iniciando servidor Uvicorn...")
        uvicorn.run("main:app", host="0.0.0.0", port=3004, reload=False)
    except Exception as e:
        logger.error(f"⚠️ ERRO CRÍTICO: O servidor falhou ao iniciar: {str(e)}")
        logger.error(f"Stack trace: {traceback.format_exc()}")
        # Aguarda um pouco antes de sair para garantir que os logs sejam gravados
        import time
        time.sleep(5)
        raise 