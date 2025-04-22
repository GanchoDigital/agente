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

# Função para adicionar logs com instância
def log_with_instance(message: str, instance_name: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    log_message = f"[{timestamp}] [{instance_name}] {message}"
    
    # Escreve no arquivo de log
    with open('bot.log', 'a', encoding='utf-8') as f:
        f.write(f"{log_message}\n")
    
    # Também usa o logger padrão
    if level == "INFO":
        logger.info(log_message)
    elif level == "ERROR":
        logger.error(log_message)
    elif level == "WARNING":
        logger.warning(log_message)
    elif level == "DEBUG":
        logger.debug(log_message)

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
                'cooldown_until': cooldown_end,
                'thread_id': contact.get('thread_id')  # Preserva o thread_id
            }).eq('whatsapp', phone).eq('instance_name', instance_name).execute()
            logger.info(f"Contato {phone} entrou em cooldown")
        else:
            supabase.table('contacts').update({
                'last_contact': datetime.utcnow().isoformat(),
                'name': push_name or contact['name'],
                'thread_id': contact.get('thread_id'),  # Preserva o thread_id
                'status': 'ativo'  # Garantindo que seja 'ativo' em vez de 'active'
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
        # Formata o número de destino
        if target_number:
            # Remove caracteres não numéricos
            target_number = ''.join(filter(str.isdigit, target_number))
            
            # Verifica formato: DDD + 9 dígitos com 9 inicial -> remove o 9
            if len(target_number) >= 3:  # Tem pelo menos DDD
                ddd = target_number[:2]
                numero = target_number[2:]
                
                if len(numero) == 9 and numero[0] == '9':
                    numero = numero[1:]  # Remove o 9 inicial
                    logger.info(f"Número após formatação: {ddd + numero}")
                
                target_number = ddd + numero
                
                # Adiciona código do país se não tiver
                if not target_number.startswith('55'):
                    target_number = '55' + target_number
                    logger.info(f"Número após adicionar código do país: {target_number}")

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
        # Normaliza as quebras de linha
        response_text = response_text.replace('\r\n', '\n').replace('\r', '\n')
        
        # Converte **texto** para *texto*
        response_text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', response_text)
        
        # Remove quebras de linha entre asteriscos mantendo o espaço
        response_text = re.sub(r'\*\s*\n\s*([^*\n]+)\s*\n\s*\*', r'* \1 *', response_text)
        
        # Se a mensagem completa for curta (menos de 100 caracteres), envia como uma única mensagem
        if len(response_text.strip()) < 100:
            logger.info(f"Mensagem curta ({len(response_text.strip())} caracteres), enviando sem dividir")
            
            evolution_url = f"{server_url}/message/sendText/{instance_name}"
            headers = {"Content-Type": "application/json", "apikey": apikey}
            payload = {"number": phone, "text": response_text.strip(), "delay": 1200}
            
            async with httpx.AsyncClient() as client:
                response = await client.post(evolution_url, headers=headers, json=payload)
                logger.info(f"Resposta da requisição: Status {response.status_code}")
                if response.status_code != 200 and response.status_code != 201:
                    logger.error(f"Erro ao enviar mensagem: {response.text}")
            return True

        # Divide o texto em blocos lógicos (parágrafos)
        # Usa uma regex que preserva caracteres especiais e monetários
        blocks = []
        current_block = ""
        
        for line in response_text.split('\n'):
            line = line.strip()
            if not line:  # Linha vazia indica quebra de bloco
                if current_block:
                    blocks.append(current_block.strip())
                    current_block = ""
            else:
                if current_block:
                    current_block += "\n" + line
                else:
                    current_block = line
        
        # Adiciona o último bloco se existir
        if current_block:
            blocks.append(current_block.strip())
        
        # Função auxiliar para verificar se um bloco contém uma lista numerada
        def contains_numbered_list(text):
            return bool(re.search(r'^\d+\.', text.strip(), re.MULTILINE))
        
        # Função auxiliar para verificar se um texto é um cabeçalho de lista
        def is_list_header(text):
            # Inclui emojis e caracteres especiais na verificação
            return bool(re.search(r'.*[:：][\s]*$', text.strip()))
        
        # Processa os blocos e mantém o contexto
        processed_blocks = []
        i = 0
        while i < len(blocks):
            current_block = blocks[i].strip()
            
            # Se o bloco atual é um cabeçalho e o próximo contém uma lista
            if i + 1 < len(blocks) and is_list_header(current_block) and contains_numbered_list(blocks[i + 1]):
                # Junta o cabeçalho com a lista
                combined_block = current_block + "\n\n" + blocks[i + 1]
                
                # Procura por mais itens de lista nos blocos seguintes
                next_index = i + 2
                while next_index < len(blocks) and contains_numbered_list(blocks[next_index]):
                    # Verifica se o próximo bloco é continuação de um item da lista
                    if re.match(r'^\d+\.', blocks[next_index].strip()):
                        combined_block += "\n" + blocks[next_index]
                    else:
                        # Se não começa com número, pode ser continuação do item anterior
                        combined_block += " " + blocks[next_index]
                    next_index += 1
                
                processed_blocks.append(combined_block)
                i = next_index
            
            # Se o bloco atual contém uma lista numerada
            elif contains_numbered_list(current_block):
                combined_block = current_block
                
                # Procura por mais itens de lista ou continuações nos blocos seguintes
                next_index = i + 1
                while next_index < len(blocks):
                    next_block = blocks[next_index].strip()
                    # Se é um novo item numerado
                    if re.match(r'^\d+\.', next_block):
                        combined_block += "\n" + next_block
                        next_index += 1
                    # Se é continuação do item anterior (não começa com número)
                    elif not re.match(r'^\d+\.', next_block) and not is_list_header(next_block):
                        combined_block += " " + next_block
                        next_index += 1
                    else:
                        break
                
                processed_blocks.append(combined_block)
                i = next_index
            
            # Bloco normal (sem lista)
            else:
                processed_blocks.append(current_block)
                i += 1
        
        # Função para limpar e formatar o texto final
        def format_block(text):
            # Remove múltiplas quebras de linha
            text = re.sub(r'\n{3,}', '\n\n', text)
            
            # Garante espaço após números de lista, preservando caracteres especiais
            text = re.sub(r'(\d+\.)([^\s])', r'\1 \2', text)
            
            # Preserva símbolos monetários e caracteres especiais
            text = re.sub(r'(R?\$)\s*(\d+)', r'\1\2', text)
            
            # Remove espaços extras no início/fim das linhas mantendo a formatação interna
            lines = []
            for line in text.split('\n'):
                if re.match(r'^\d+\.', line.strip()):  # Se é item de lista
                    lines.append(line.strip())
                else:
                    lines.append(line.strip())
            
            return '\n'.join(lines)
        
        # Formata os blocos processados
        formatted_blocks = [format_block(block) for block in processed_blocks]
        
        # Remove blocos vazios
        formatted_blocks = [block for block in formatted_blocks if block.strip()]
        
        # Log para debug
        logger.info(f"Mensagem dividida em {len(formatted_blocks)} blocos:")
        for i, block in enumerate(formatted_blocks):
            logger.info(f"Bloco {i+1}:\n{block}")
        
        # Envia os blocos
        async with httpx.AsyncClient() as client:
            for i, message in enumerate(formatted_blocks):
                logger.info(f"Enviando bloco {i+1}/{len(formatted_blocks)}")
                
                evolution_url = f"{server_url}/message/sendText/{instance_name}"
                headers = {"Content-Type": "application/json", "apikey": apikey}
                payload = {"number": phone, "text": message, "delay": 1200}
                
                response = await client.post(evolution_url, headers=headers, json=payload)
                logger.info(f"Resposta da requisição {i+1}: Status {response.status_code}")
                
                if response.status_code != 200 and response.status_code != 201:
                    logger.error(f"Erro ao enviar mensagem: {response.text}")
                
                # Pequena pausa entre mensagens
                await asyncio.sleep(1)
        
        logger.info("Todas as mensagens foram enviadas com sucesso")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao dividir e enviar mensagens: {str(e)}")
        logger.error(traceback.format_exc())
        return False

async def send_transfer_request(
    phone: str,
    client_name: str,
    reason: str,
    instance_name: str,
    apikey: str,
    server_url: str
):
    """Solicita transferência do atendimento para um humano."""
    try:
        # Busca o usuário responsável pelo assistente
        logger.info(f"Preparando transferência para humano. Instância: {instance_name}")
        assistant_data = supabase.table('assistants').select(
            'id, user_id'
        ).eq('instance_name', instance_name).execute()
        
        if not assistant_data.data or len(assistant_data.data) == 0:
            logger.error(f"Assistente não encontrado: {instance_name}")
            return False
        
        user_id = assistant_data.data[0]['user_id']
        assistant_id = assistant_data.data[0]['id']
        
        logger.info(f"ID do assistente encontrado: {assistant_id}")
        
        # Busca o número de transferência na tabela agent_configurations
        transfer_config = supabase.table('agent_configurations').select(
            'transfer_number'
        ).eq('assistant_id', assistant_id).execute()
        
        if not transfer_config.data or len(transfer_config.data) == 0:
            logger.error(f"Configuração de transferência não encontrada para assistente: {assistant_id}")
            transfer_number = None
        else:
            transfer_number = transfer_config.data[0]['transfer_number']
            logger.info(f"Número de transferência encontrado: {transfer_number}")
            
            # Formata o número conforme regra
            if transfer_number:
                # Remove caracteres não numéricos
                transfer_number = ''.join(filter(str.isdigit, transfer_number))
                
                # Verifica formato: DDD + 9 dígitos com 9 inicial -> remove o 9
                if len(transfer_number) >= 3:  # Tem pelo menos DDD
                    ddd = transfer_number[:2]
                    numero = transfer_number[2:]
                    
                    if len(numero) == 9 and numero[0] == '9':
                        numero = numero[1:]  # Remove o 9 inicial
                        logger.info(f"Número após formatação: {ddd + numero}")
                    
                    transfer_number = ddd + numero
                    
                    # Adiciona código do país se não tiver
                    if not transfer_number.startswith('55'):
                        transfer_number = '55' + transfer_number
                        logger.info(f"Número após adicionar código do país: {transfer_number}")
        
        # Atualiza o status do contato para "cooldown"
        try:
            supabase.table('contacts').update({
                'status': 'cooldown',
                'transfer_reason': reason,
                'cooldown_until': (datetime.utcnow() + timedelta(hours=24)).isoformat()
            }).eq('whatsapp', phone).eq('instance_name', instance_name).execute()
            logger.info(f"Status do contato {phone} atualizado para 'cooldown'")
        except Exception as e:
            logger.error(f"Erro ao atualizar status do contato: {str(e)}")
        
        # Notifica o usuário sobre a solicitação
        transfer_message = (
            "Estou transferindo seu atendimento para um humano. Em breve alguém entrará em contato."
        )
        
        evolution_url = f"{server_url}/message/sendText/{instance_name}"
        
        headers = {
            "Content-Type": "application/json",
            "apikey": apikey
        }
        
        payload = {
            "number": phone,
            "text": transfer_message,
            "delay": 1200
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                evolution_url,
                headers=headers,
                json=payload
            )
            
            if response.status_code != 200 and response.status_code != 201:
                logger.error(f"Erro ao enviar mensagem de transferência: {response.text}")
                return False
        
        # Envia notificação para o número de transferência
        if transfer_number:
            await send_notification(
                target_number=transfer_number,
                client_number=phone,
                client_name=client_name,
                context=reason,
                instance_name=instance_name,
                apikey=apikey,
                server_url=server_url
            )
        
        logger.info(f"Solicitação de transferência processada para {phone}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao processar transferência: {str(e)}")
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

async def send_image(phone: str, instance_name: str, apikey: str, server_url: str, media_id: str) -> bool:
    """Envia uma imagem para o usuário usando o ID da mídia do banco de dados."""
    try:
        # Busca a imagem no banco de dados
        response = supabase.table('media').select('link').eq('media_id', media_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Imagem não encontrada com ID: {media_id}")
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

async def send_audio(phone: str, instance_name: str, apikey: str, server_url: str, media_id: str) -> bool:
    """Envia um áudio para o usuário usando o ID da mídia do banco de dados."""
    try:
        # Busca o áudio no banco de dados
        response = supabase.table('media').select('link').eq('media_id', media_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Áudio não encontrado com ID: {media_id}")
            return False
            
        audio_link = response.data[0]['link']
        logger.info(f"Link do áudio encontrado: {audio_link}")
        
        # Prepara a requisição para a Evolution API
        evolution_url = f"{server_url}/message/sendWhatsAppAudio/{instance_name}"
        
        headers = {
            "Content-Type": "application/json",
            "apikey": apikey
        }
        
        payload = {
            "number": phone,
            "audio": audio_link,
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
                logger.error(f"Erro ao enviar áudio: {response.text}")
                return False
                
        logger.info(f"Áudio enviado com sucesso para {phone}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao enviar áudio: {str(e)}")
        return False

async def send_video(phone: str, instance_name: str, apikey: str, server_url: str, media_id: str) -> bool:
    """Envia um vídeo para o usuário usando o ID da mídia do banco de dados."""
    try:
        # Busca o vídeo no banco de dados
        response = supabase.table('media').select('link').eq('media_id', media_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Vídeo não encontrado com ID: {media_id}")
            return False
            
        video_link = response.data[0]['link']
        logger.info(f"Link do vídeo encontrado: {video_link}")
        
        # Prepara a requisição para a Evolution API
        evolution_url = f"{server_url}/message/sendMedia/{instance_name}"
        
        headers = {
            "Content-Type": "application/json",
            "apikey": apikey
        }
        
        payload = {
            "number": phone,
            "mediatype": "video",
            "mimetype": "video/mp4",
            "media": video_link,
            "fileName": "video.mp4",
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
                logger.error(f"Erro ao enviar vídeo: {response.text}")
                return False
                
        logger.info(f"Vídeo enviado com sucesso para {phone}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao enviar vídeo: {str(e)}")
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
        
        if not contact:
            logger.info(f"Contato {phone} não encontrado")
            return
            
        if contact['status'] in ['cooldown', 'pausado']:
            logger.info(f"Contato {phone} está {contact['status']}")
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
                        
                        if function_name == 'solicitar_transferencia':
                            # Obtém informações do contato
                            contact = await check_and_create_contact(phone, instance_name, "", False)
                            client_name = contact.get('name', 'Nome não disponível')
                            
                            # Obtém o motivo da transferência
                            reason = function_args.get('motivo', 'Não especificado')
                            
                            # Solicita a transferência
                            success = await send_transfer_request(
                                phone=phone,
                                client_name=client_name,
                                reason=reason,
                                instance_name=instance_name,
                                apikey=apikey,
                                server_url=server_url
                            )
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": "Transferência solicitada com sucesso" if success else "Falha ao solicitar transferência"
                                })
                            })
                            
                            logger.info(f"Função solicitar_transferencia processada para {phone}: {reason}")
                            
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
                            media_id = function_args.get('media_id')
                            
                            # Envia a imagem
                            success = await send_image(
                                phone=phone,
                                instance_name=instance_name,
                                apikey=apikey,
                                server_url=server_url,
                                media_id=media_id
                            )
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": f"Imagem enviada com sucesso" if success else "Falha ao enviar imagem"
                                })
                            })
                            
                            logger.info(f"Função enviar_imagem processada para {phone}: {media_id}")
                            
                        elif function_name == 'enviar_audio':
                            media_id = function_args.get('media_id')
                            
                            # Envia o áudio
                            success = await send_audio(
                                phone=phone,
                                instance_name=instance_name,
                                apikey=apikey,
                                server_url=server_url,
                                media_id=media_id
                            )
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": f"Áudio enviado com sucesso" if success else "Falha ao enviar áudio"
                                })
                            })
                            
                            logger.info(f"Função enviar_audio processada para {phone}: {media_id}")
                            
                        elif function_name == 'enviar_video':
                            media_id = function_args.get('media_id')
                            
                            # Envia o vídeo
                            success = await send_video(
                                phone=phone,
                                instance_name=instance_name,
                                apikey=apikey,
                                server_url=server_url,
                                media_id=media_id
                            )
                            
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": f"Vídeo enviado com sucesso" if success else "Falha ao enviar vídeo"
                                })
                            })
                            
                            logger.info(f"Função enviar_video processada para {phone}: {media_id}")
                            
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
        log_with_instance("Nova requisição recebida no webhook", data.instance)
        log_with_instance(f"Dados recebidos: {data}", data.instance)
        
        # Verifica se é um evento de mensagem
        if data.event != "messages.upsert":
            log_with_instance(f"Evento ignorado: {data.event}", data.instance)
            return {"success": True, "message": "Evento ignorado"}

        # Extrai informações relevantes
        phone = data.data.key.remoteJid.split('@')[0]
        instance_name = data.instance
        push_name = data.data.pushName
        from_me = data.data.key.fromMe
        message_type = data.data.messageType
        
        log_with_instance(f"Processando mensagem de {phone}", instance_name)
        log_with_instance(f"Tipo de mensagem: {message_type}", instance_name)
        log_with_instance(f"De mim: {from_me}", instance_name)

        # Primeiro, verifica o status atual do contato
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('instance_name', instance_name).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        log_with_instance(f"Status atual do contato {phone}: {current_status}", instance_name)

        # Se a mensagem é do usuário (fromMe = true)
        if from_me:
            log_with_instance(f"Mensagem enviada pelo usuário para {phone}", instance_name)
            try:
                # Se o status atual é 'pausado', mantém pausado
                if current_status == 'pausado':
                    log_with_instance(f"Contato {phone} mantido como pausado", instance_name)
                    return {"success": True, "message": "Contato mantido como pausado"}
                
                # Caso contrário, atualiza para cooldown
                cooldown_end = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                supabase.table('contacts').update({
                    'last_contact': datetime.utcnow().isoformat(),
                    'status': 'cooldown',
                    'cooldown_until': cooldown_end,
                    'from_me': True
                }).eq('whatsapp', phone).eq('instance_name', instance_name).execute()
                
                log_with_instance(f"Contato {phone} colocado em cooldown por 24 horas", instance_name)
                return {"success": True, "message": "Contato em cooldown após mensagem do usuário"}
            except Exception as e:
                log_with_instance(f"Erro ao atualizar status do contato: {str(e)}", instance_name, "ERROR")
                return {"success": False, "message": "Erro ao atualizar status do contato"}

        # Verifica novamente o status após qualquer atualização
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('instance_name', instance_name).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        log_with_instance(f"Status verificado novamente para {phone}: {current_status}", instance_name)

        # Se o contato está em cooldown ou pausado, não processa a mensagem
        if current_status in ['cooldown', 'pausado']:
            log_with_instance(f"Mensagem descartada para {phone}. Status: {current_status}", instance_name)
            return {"success": False, "message": f"Contato {current_status}"}

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

        # Verifica o status uma última vez antes de processar a mensagem
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('instance_name', instance_name).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        if current_status in ['cooldown', 'pausado']:
            logger.info(f"Mensagem descartada para {phone}. Status final: {current_status}")
            return {"success": False, "message": f"Contato {current_status}"}

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

        # Verifica o status uma última vez antes de adicionar à fila
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('instance_name', instance_name).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        if current_status in ['cooldown', 'pausado']:
            logger.info(f"Mensagem descartada para {phone}. Status final antes da fila: {current_status}")
            return {"success": False, "message": f"Contato {current_status}"}

        # Adiciona a mensagem à lista de mensagens pendentes
        key = f"{phone}:{instance_name}"
        if key not in pending_messages:
            pending_messages[key] = []
        
        pending_messages[key].append(user_message)
        log_with_instance(f"Mensagem adicionada à fila para {phone}. Total: {len(pending_messages[key])}", instance_name)
        
        # Se já existe uma tarefa pendente para este contato, não cria outra
        if key in pending_tasks and not pending_tasks[key].done():
            log_with_instance(f"Já existe uma tarefa pendente para {key}", instance_name)
            return {"success": True, "message": "Mensagem adicionada à fila existente"}
            
        # Cria uma nova tarefa para processar após 5 segundos
        task = asyncio.create_task(
            process_delayed_message(phone, instance_name, data.apikey, data.server_url)
        )
        pending_tasks[key] = task
        log_with_instance(f"Nova tarefa de processamento criada para {key}", instance_name)

        return {"success": True, "message": "Mensagem adicionada à fila"}

    except Exception as e:
        log_with_instance(f"Erro no processamento: {str(e)}", data.instance, "ERROR")
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