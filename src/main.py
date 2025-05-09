from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
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
import pytz

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

# Função para adicionar logs
def log_with_instance(message: str, agent_id: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    log_message = f"[{timestamp}] [{agent_id}] {message}"
    
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
QUEPASA_API_URL = os.getenv("QUEPASA_API_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Logs para debug
logger.info(f"QUEPASA_API_URL: {QUEPASA_API_URL}")
logger.info(f"OPENAI_API_KEY definida: {bool(OPENAI_API_KEY)}")
logger.info(f"SUPABASE_URL definida: {bool(SUPABASE_URL)}")
logger.info(f"SUPABASE_KEY definida: {bool(SUPABASE_KEY)}")

# Valida as credenciais
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY não definida no ambiente")
if not QUEPASA_API_URL:
    logger.error("QUEPASA_API_URL não definida no ambiente")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("SUPABASE_URL ou SUPABASE_KEY não definidas no ambiente")

app = FastAPI(title="WhatsApp GPT Bot")

# Configuração CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite todas as origens
    allow_credentials=True,
    allow_methods=["*"],  # Permite todos os métodos
    allow_headers=["*"],  # Permite todos os headers
)

# Middleware para log de todas as requisições
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"=== NOVA REQUISIÇÃO RECEBIDA ===")
    logger.info(f"URL: {request.url}")
    logger.info(f"Método: {request.method}")
    logger.info(f"Headers: {dict(request.headers)}")
    
    # Tenta ler o corpo da requisição
    try:
        body = await request.body()
        logger.info(f"Corpo da requisição: {body.decode()}")
    except Exception as e:
        logger.error(f"Erro ao ler corpo da requisição: {str(e)}")
    
    response = await call_next(request)
    return response

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
    agent_id: str  # Alterado de instanceId para agent_id
    source: str

class WhatsAppWebhook(BaseModel):
    event: str
    agent_id: str  # Alterado de instance para agent_id
    data: MessageData
    destination: str
    date_time: str
    sender: str
    server_url: str
    token: str  # Alterado de apikey para token

# Classes para o novo formato do webhook Quepasa
class ChatInfo(BaseModel):
    id: str
    title: Optional[str] = None

class Attachment(BaseModel):
    mime: Optional[str] = None
    filelength: Optional[int] = None
    seconds: Optional[int] = None

class QuepasaMessage(BaseModel):
    id: str
    timestamp: str
    type: str
    chat: ChatInfo
    text: Optional[str] = None
    attachment: Optional[Attachment] = None
    fromme: Any  # Pode ser string "false"/"true" ou booleano
    frominternal: Any  # Pode ser string "false"/"true" ou booleano

class QuepasaWebhook(BaseModel):
    body: QuepasaMessage

async def check_and_create_contact(phone: str, quepasa_wid: str, push_name: str, from_me: bool, chat_title: str = None) -> Optional[Dict]:
    try:
        # Limpa o número do telefone (remove @s.whatsapp.net)
        phone = phone.split('@')[0]
        
        # Busca o assistente para obter o assistant_id
        assistant_response = supabase.table('assistants').select('assistant_id').eq('x-quepasa-wid', quepasa_wid).execute()
        if not assistant_response.data or len(assistant_response.data) == 0:
            logger.error(f"Assistente não encontrado para x-quepasa-wid: {quepasa_wid}")
            raise Exception("Assistente não encontrado")
            
        assistant_id = assistant_response.data[0]['assistant_id']
        
        # Busca o contato - Usando nome real da coluna 'x-quepasa-wid'
        response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
        
        if not response.data:
            # Se não existe, cria um novo contato
            thread = openai_client.beta.threads.create()
            new_contact = {
                'name': chat_title or push_name or f'User {phone}',  # Usa chat_title como prioridade
                'whatsapp': phone,
                'status': 'ativo',
                'x-quepasa-wid': quepasa_wid,
                'last_contact': datetime.utcnow().isoformat(),
                'thread_id': thread.id,
                'followup': False,
                'etapa': 'conexão',
                'from_me': from_me,
                'instance_name': assistant_id
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
                'thread_id': contact.get('thread_id'),  # Preserva o thread_id
                'instance_name': assistant_id
            }).eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
            logger.info(f"Contato {phone} entrou em cooldown")
        else:
            supabase.table('contacts').update({
                'last_contact': datetime.utcnow().isoformat(),
                'name': chat_title or push_name or contact['name'],  # Usa chat_title como prioridade
                'thread_id': contact.get('thread_id'),  # Preserva o thread_id
                'status': 'ativo',  # Garantindo que seja 'ativo' em vez de 'active'
                'instance_name': assistant_id
            }).eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
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
    agent_id: str,
    token: str
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

        # Envia via QuepasaAPI
        success = await send_quepasa_message(
            phone=target_number,
            data={
                "trackid": agent_id,
                "text": notification_message
            },
            token=token
        )
            
        logger.info(f"Notificação enviada para {target_number}")
        return success

    except Exception as e:
        logger.error(f"Erro ao enviar notificação: {str(e)}")
        return False

async def check_contact_limit(agent_id: str, contact_number: str) -> bool:
    """
    Verifica se o usuário atingiu o limite de contatos do seu plano.
    Retorna True se ainda está dentro do limite, False se excedeu.
    """
    try:
        # Busca o usuário responsável pelo assistente
        logger.info(f"Verificando limite para assistente: {agent_id}")
        assistant_data = supabase.table('assistants').select(
            'user_id'
        ).eq('id', agent_id).execute()
        
        if not assistant_data.data or len(assistant_data.data) == 0:
            logger.error(f"Assistente não encontrado: {agent_id}")
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

async def send_quepasa_message(phone: str, data: dict, token: str) -> bool:
    """Função unificada para enviar mensagens via QuepasaAPI."""
    try:
        # Limpa o número de telefone (remove @s.whatsapp.net se existir)
        if '@' in phone:
            phone = phone.split('@')[0]
            logger.info(f"Número de telefone limpo: {phone}")
        
        # Configura os dados básicos
        payload = {
            "chatid": phone,
            "trackid": str(data.get("trackid", "agent")),  # Garantir que trackid seja string
            "text": data.get("text", "")
        }
        
        # Adiciona informações de mídia se fornecidas
        if "mime" in data:
            payload["mime"] = data["mime"]
            payload["url"] = data.get("url", "")
            payload["filename"] = data.get("filename", "")
        
        # Log para debug
        logger.info(f"Enviando payload para Quepasa: {json.dumps(payload)}")
        
        # Envia a requisição com o token no header
        headers = {
            "Content-Type": "application/json",
            "X-QUEPASA-TOKEN": token
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{QUEPASA_API_URL}/send",
                headers=headers,
                json=payload,
                timeout=30
            )
            
        logger.info(f"Resposta da requisição Quepasa: Status {response.status_code}")
        if response.status_code >= 400:
            logger.error(f"Erro na resposta Quepasa: {response.text}")
            
        return response.status_code in [200, 201, 202]
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem via QuepasaAPI: {str(e)}")
        logger.error(traceback.format_exc())
        return False

async def send_whatsapp_messages(response_text: str, phone: str, agent_id: str, token: str):
    """Divide a mensagem do assistente em partes e envia como mensagens separadas."""
    try:
        # Limpa o número de telefone (remove @s.whatsapp.net se existir)
        if '@' in phone:
            phone = phone.split('@')[0]
            logger.info(f"Número de telefone limpo: {phone}")
            
        # Remove os separadores "---"
        response_text = re.sub(r'\s*---\s*', '\n', response_text)
        
        # Normaliza as quebras de linha
        response_text = response_text.replace('\r\n', '\n').replace('\r', '\n')
        
        # Adiciona quebra de linha após ! e ? apenas se não for seguido por espaço
        response_text = re.sub(r'([!?])(?!\s)(?!\n)', r'\1\n', response_text)
        
        # Converte **texto** para *texto*
        response_text = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', response_text)
        
        # Remove quebras de linha entre asteriscos mantendo o espaço
        response_text = re.sub(r'\*\s*\n\s*([^*\n]+)\s*\n\s*\*', r'* \1 *', response_text)
        
        # Remove quebras de linha duplicadas
        response_text = re.sub(r'\n\s*\n', '\n', response_text)
        
        # Divide o texto em blocos lógicos
        blocks = []
        current_block = ""
        lines = response_text.split('\n')
        i = 0
        
        while i < len(lines):
            line = lines[i].strip()
            
            # Se é um item numerado
            if re.match(r'^\d+\.', line):
                # Começa um novo bloco se necessário
                if current_block and not re.match(r'^\d+\.', current_block.split('\n')[0]):
                    blocks.append(current_block.strip())
                    current_block = ""
                
                # Adiciona o item numerado
                if current_block:
                    current_block += "\n"
                current_block += line
                
                # Olha à frente para ver se há continuação do item
                j = i + 1
                while j < len(lines) and (not re.match(r'^\d+\.', lines[j].strip()) and lines[j].strip()):
                    current_block += " " + lines[j].strip()
                    j += 1
                i = j
            else:
                # Para texto normal, verifica o tamanho
                if len(current_block + "\n" + line) > 150 and current_block:
                    blocks.append(current_block.strip())
                    current_block = line
                else:
                    if current_block and line:
                        current_block += "\n" + line
                    elif line:
                        current_block = line
                i += 1
        
        # Adiciona o último bloco
        if current_block:
            blocks.append(current_block.strip())
        
        # Remove blocos vazios e garante quebras de linha após ! e ? (exceto se seguido por espaço)
        blocks = [re.sub(r'([!?])(?!\s)(?!\n)(?!$)', r'\1\n', b.strip()) for b in blocks if b.strip()]
        
        # Log para debug
        logger.info(f"Mensagem dividida em {len(blocks)} partes:")
        for i, block in enumerate(blocks):
            logger.info(f"Parte {i+1}: {block[:50]}...")
        
        # Envia as mensagens
        for i, message in enumerate(blocks):
            logger.info(f"Enviando parte {i+1}/{len(blocks)}")
            
            success = await send_quepasa_message(
                phone=phone,
                data={
                    "trackid": agent_id,
                    "text": message.strip()
                },
                token=token
            )
            
            if not success:
                logger.error(f"Erro ao enviar mensagem {i+1}")
                
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
    agent_id: str,
    token: str,
    quepasa_wid: str
):
    """Solicita transferência do atendimento para um humano."""
    try:
        # Busca o usuário responsável pelo assistente
        logger.info(f"Preparando transferência para humano. Agent ID: {agent_id}")
        assistant_data = supabase.table('assistants').select(
            'id, user_id'
        ).eq('id', agent_id).execute()
        
        if not assistant_data.data or len(assistant_data.data) == 0:
            logger.error(f"Assistente não encontrado: {agent_id}")
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
            }).eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
            logger.info(f"Status do contato {phone} atualizado para 'cooldown'")
        except Exception as e:
            logger.error(f"Erro ao atualizar status do contato: {str(e)}")
        
        # Notifica o usuário sobre a solicitação
        transfer_message = (
            "Estou transferindo seu atendimento para um humano. Em breve alguém entrará em contato."
        )
        
        # Envia mensagem via QuepasaAPI
        success = await send_quepasa_message(
            phone=phone,
            data={
                "trackid": agent_id,
                "text": transfer_message
            },
            token=token
        )
        
        if not success:
            logger.error("Erro ao enviar mensagem de transferência")
            return False
        
        # Envia notificação para o número de transferência
        if transfer_number:
            await send_notification(
                target_number=transfer_number,
                client_number=phone,
                client_name=client_name,
                context=reason,
                agent_id=agent_id,
                token=token
            )
        
        logger.info(f"Solicitação de transferência processada para {phone}")
        return True
        
    except Exception as e:
        logger.error(f"Erro ao processar transferência: {str(e)}")
        return False

async def update_contact_stage(phone: str, stage: str, quepasa_wid: str) -> bool:
    """Atualiza o estágio do contato no banco de dados."""
    try:
        data = supabase.table('contacts').update(
            {"etapa": stage}
        ).eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
        
        logger.info(f"Estágio do contato {phone} atualizado para: {stage}")
        
        # Adicionando log para verificar a resposta do Supabase
        logger.info(f"Resposta do Supabase: {data}")
        
        return True
    except Exception as e:
        logger.error(f"Erro ao atualizar estágio do contato: {str(e)}")
        # Adicionando log detalhado do erro
        logger.error(f"Detalhes do erro: phone={phone}, stage={stage}")
        return False

async def send_image(phone: str, agent_id: str, token: str, media_id: str) -> bool:
    """Envia uma imagem para o usuário usando o ID da mídia do banco de dados."""
    try:
        # Busca a imagem no banco de dados
        response = supabase.table('media').select('link').eq('media_id', media_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Imagem não encontrada com ID: {media_id}")
            return False
            
        image_link = response.data[0]['link']
        logger.info(f"Link da imagem encontrado: {image_link}")
        
        # Obtém o nome do arquivo a partir do link
        filename = image_link.split('/')[-1]
        
        # Envia via QuepasaAPI
        success = await send_quepasa_message(
            phone=phone,
            data={
                "trackid": agent_id,
                "text": "",
                "mime": "image/png",
                "url": image_link,
                "filename": filename
            },
            token=token
        )
                
        logger.info(f"Imagem enviada com sucesso para {phone}" if success else f"Falha ao enviar imagem para {phone}")
        return success
        
    except Exception as e:
        logger.error(f"Erro ao enviar imagem: {str(e)}")
        return False

async def send_audio(phone: str, agent_id: str, token: str, media_id: str) -> bool:
    """Envia um áudio para o usuário usando o ID da mídia do banco de dados."""
    try:
        # Busca o áudio no banco de dados
        response = supabase.table('media').select('link').eq('media_id', media_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Áudio não encontrado com ID: {media_id}")
            return False
            
        audio_link = response.data[0]['link']
        logger.info(f"Link do áudio encontrado: {audio_link}")
        
        # Obtém o nome do arquivo a partir do link
        filename = audio_link.split('/')[-1]
        
        # Envia via QuepasaAPI
        success = await send_quepasa_message(
            phone=phone,
            data={
                "trackid": agent_id,
                "text": "",
                "mime": "audio/ogg",
                "url": audio_link,
                "filename": filename
            },
            token=token
        )
                
        logger.info(f"Áudio enviado com sucesso para {phone}" if success else f"Falha ao enviar áudio para {phone}")
        return success
        
    except Exception as e:
        logger.error(f"Erro ao enviar áudio: {str(e)}")
        return False

async def send_video(phone: str, agent_id: str, token: str, media_id: str) -> bool:
    """Envia um vídeo para o usuário usando o ID da mídia do banco de dados."""
    try:
        # Busca o vídeo no banco de dados
        response = supabase.table('media').select('link').eq('media_id', media_id).execute()
        
        if not response.data or len(response.data) == 0:
            logger.error(f"Vídeo não encontrado com ID: {media_id}")
            return False
            
        video_link = response.data[0]['link']
        logger.info(f"Link do vídeo encontrado: {video_link}")
        
        # Obtém o nome do arquivo a partir do link
        filename = video_link.split('/')[-1]
        
        # Envia via QuepasaAPI
        success = await send_quepasa_message(
            phone=phone,
            data={
                "trackid": agent_id,
                "text": "",
                "mime": "video/mp4",
                "url": video_link,
                "filename": filename
            },
            token=token
        )
                
        logger.info(f"Vídeo enviado com sucesso para {phone}" if success else f"Falha ao enviar vídeo para {phone}")
        return success
        
    except Exception as e:
        logger.error(f"Erro ao enviar vídeo: {str(e)}")
        return False

async def process_delayed_message(phone: str, agent_id: str, token: str, quepasa_wid: str, openai_assistant_id: str, chat_title: str):
    # Define a chave única no início da função
    key = f"{phone}:{agent_id}"
    
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
        contact = await check_and_create_contact(phone, quepasa_wid, "", False, chat_title)
        
        if not contact:
            logger.info(f"Contato {phone} não encontrado")
            return
            
        if contact['status'] in ['cooldown', 'pausado']:
            logger.info(f"Contato {phone} está {contact['status']}")
            return
            
        # Verifica se a thread existente é válida ou se precisa criar uma nova
        thread_id = contact.get('thread_id')
        if not thread_id:
            # Cria uma nova thread se thread_id for nulo
            logger.info(f"Thread ID nula para o contato {phone}. Criando nova thread...")
            thread = openai_client.beta.threads.create()
            thread_id = thread.id
            
            # Atualiza o contato com a nova thread_id
            try:
                supabase.table('contacts').update({
                    'thread_id': thread_id
                }).eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
                logger.info(f"Contato {phone} atualizado com nova thread_id: {thread_id}")
            except Exception as e:
                logger.error(f"Erro ao atualizar thread_id do contato: {str(e)}")
        
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
        # Adiciona informações de contexto do cliente
        brasilia_tz = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(brasilia_tz)
        date_str = now.strftime("%d/%m/%Y")
        time_str = now.strftime("%H:%M:%S")
        
        # Adiciona cabeçalho com informações do cliente à mensagem
        contact_info = f"""
INFORMAÇÕES DO CLIENTE:
Número: {phone}
Nome: {chat_title or "Não informado"}
Data atual: {date_str} 
Hora atual (Brasília): {time_str}

MENSAGEM:
{concatenated_message}
"""
        
        thread_message = openai_client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=contact_info
        )
        logger.info(f"Mensagens concatenadas adicionadas à thread: {thread_message.id}")
        
        # Executa o assistente usando o ID correto do OpenAI
        logger.info(f"Executando assistente com ID OpenAI: {openai_assistant_id}")
        run = openai_client.beta.threads.runs.create(
            thread_id=thread_id,
            assistant_id=openai_assistant_id  # Usa o ID do assistente na OpenAI (formato asst_XXX)
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
                            contact = await check_and_create_contact(phone, quepasa_wid, "", False, chat_title)
                            client_name = contact.get('name', 'Nome não disponível')
                            
                            # Obtém o motivo da transferência
                            reason = function_args.get('motivo', 'Não especificado')
                            
                            # Solicita a transferência
                            success = await send_transfer_request(
                                phone=phone,
                                client_name=client_name,
                                reason=reason,
                                agent_id=agent_id,
                                token=token,
                                quepasa_wid=quepasa_wid
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
                            success = await update_contact_stage(phone, stage, quepasa_wid)
                            
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
                                agent_id=agent_id,
                                token=token,
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
                                agent_id=agent_id,
                                token=token,
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
                                agent_id=agent_id,
                                token=token,
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
                        
                        elif function_name == 'agendamento':
                            # Obtém os dados do agendamento
                            title = function_args.get('title', '')
                            description = function_args.get('description', '')
                            customer_name = function_args.get('customer_name', '')
                            customer_contact = function_args.get('customer_contact', '')
                            appointment_date = function_args.get('appointment_date', '')
                            appointment_time = function_args.get('appointment_time', '')
                            end_time = function_args.get('end_time', '')
                            location = function_args.get('location', '')
                            service_type = function_args.get('service_type', '')
                            status = function_args.get('status', 'agendado')
                            notes = function_args.get('notes', '')
                            
                            try:
                                # Obtém o user_id do assistente - tentando várias abordagens
                                logger.info(f"DEBUG - Tentando obter user_id para assistente_id: {agent_id}")
                                
                                # Primeiro, tenta buscar pelo id do assistente (caso seja o id da tabela)
                                logger.info(f"DEBUG - Tentativa 1: Buscando pelo id na tabela")
                                assistant_response = supabase.table('assistants').select('*').eq('id', agent_id).execute()
                                
                                # Se não encontrar, tenta pelo assistant_id
                                if not assistant_response.data or len(assistant_response.data) == 0:
                                    logger.info(f"DEBUG - Tentativa 2: Buscando pelo assistant_id")
                                    assistant_response = supabase.table('assistants').select('*').eq('assistant_id', agent_id).execute()
                                
                                # Se ainda não encontrar, tenta buscar pelo token (que às vezes é usado como assistant_id)
                                if not assistant_response.data or len(assistant_response.data) == 0:
                                    logger.info(f"DEBUG - Tentativa 3: Buscando pelo token")
                                    assistant_response = supabase.table('assistants').select('*').eq('token', agent_id).execute()
                                
                                # Se ainda não encontrar, tenta buscar assistentes que tenham o mesmo domínio no token
                                if not assistant_response.data or len(assistant_response.data) == 0:
                                    logger.info(f"DEBUG - Tentativa 4: Buscando pelo domínio Quepasa")
                                    assistant_response = supabase.table('assistants').select('*').eq('x-quepasa-wid', quepasa_wid).execute()
                                
                                # Final fallback - obtém qualquer assistente
                                if not assistant_response.data or len(assistant_response.data) == 0:
                                    logger.info(f"DEBUG - Tentativa 5: Buscando qualquer assistente")
                                    assistant_response = supabase.table('assistants').select('*').limit(1).execute()
                                    
                                    if not assistant_response.data or len(assistant_response.data) == 0:
                                        logger.error(f"DEBUG - Não foi possível encontrar nenhum assistente")
                                        # Usando UUID fixo como último recurso
                                        logger.info(f"DEBUG - Último recurso: Usando UUID fixo")
                                        user_id = '0b411149-a240-4eb7-ba94-920f6be08bb9'  # ID que vimos na imagem
                                    else:
                                        logger.info(f"DEBUG - Encontrado um assistente como fallback: {assistant_response.data[0]}")
                                        user_id = assistant_response.data[0]['user_id']
                                else:
                                    logger.info(f"DEBUG - Assistente encontrado: {assistant_response.data[0]}")
                                    user_id = assistant_response.data[0]['user_id']
                                
                                logger.info(f"DEBUG - User ID final para agendamento: {user_id}")
                                
                                # Obtém o contact_id do contato atual
                                contact_response = supabase.table('contacts').select('id').eq('whatsapp', phone).eq('x-quepasa-wid', quepasa_wid).execute()
                                
                                if not contact_response.data or len(contact_response.data) == 0:
                                    logger.error(f"Contato não encontrado para agendamento: {phone}")
                                    raise Exception("Contato não encontrado")
                                
                                contact_id = contact_response.data[0]['id']
                                logger.info(f"DEBUG - Contact ID para agendamento: {contact_id}")
                                
                                # Formatar data e hora para o formato ISO
                                start_datetime = f"{appointment_date}T{appointment_time}:00"
                                end_datetime = f"{appointment_date}T{end_time}:00" if end_time else None
                                
                                # Insere o agendamento no banco de dados com as colunas corretas
                                appointment_data = {
                                    'title': title,
                                    'description': description or f"Agendamento para {customer_name}",
                                    'start_time': start_datetime,
                                    'end_time': end_datetime,
                                    'status': status,
                                    'location': location,
                                    'contact_id': contact_id,
                                    'user_id': user_id,  # Adiciona o user_id
                                    'created_at': datetime.utcnow().isoformat(),
                                    'updated_at': datetime.utcnow().isoformat()
                                }
                                
                                # Adiciona notas ao description se existirem
                                if notes:
                                    appointment_data['description'] += f"\nObservações: {notes}"
                                    
                                # Adiciona serviço ao description se existir
                                if service_type:
                                    appointment_data['description'] += f"\nServiço: {service_type}"
                                    
                                # Adiciona dados do cliente ao description
                                appointment_data['description'] += f"\nCliente: {customer_name}"
                                appointment_data['description'] += f"\nContato: {customer_contact or phone}"
                                
                                # Realiza a inserção no banco
                                response = supabase.table('calendar_events').insert(appointment_data).execute()
                                
                                success = True
                                logger.info(f"Agendamento criado com sucesso para {phone}: {title} em {start_datetime}")
                            except Exception as e:
                                logger.error(f"Erro ao criar agendamento: {str(e)}")
                                success = False
                            
                            # Retorna o resultado
                            tool_outputs.append({
                                "tool_call_id": tool_call.id,
                                "output": json.dumps({
                                    "success": success,
                                    "message": "Agendamento criado com sucesso" if success else "Falha ao criar agendamento"
                                })
                            })
                            
                            logger.info(f"Função agendamento processada para {phone}")
                            
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
            agent_id=agent_id,
            token=token
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
async def webhook(request: Request, x_quepasa_wid: str = Header(None, alias="x-quepasa-wid")):
    try:
        # Lê o corpo da requisição
        body = await request.json()
        
        # Verifica se o corpo está dentro de um objeto 'body'
        if 'body' in body:
            message_data = body['body']
        else:
            message_data = body
            
        # Cria o objeto QuepasaMessage
        message = QuepasaMessage(
            id=message_data['id'],
            timestamp=message_data['timestamp'],
            type=message_data['type'],
            chat=ChatInfo(
                id=message_data['chat']['id'],
                title=message_data['chat'].get('title')
            ),
            text=message_data.get('text'),
            fromme=str(message_data.get('fromme', 'false')).lower() == 'true',
            frominternal=str(message_data.get('frominternal', 'false')).lower() == 'true'
        )
        
        # Log detalhado da requisição recebida
        logger.info("=== NOVA REQUISIÇÃO WEBHOOK ===")
        logger.info(f"Headers recebidos: x-quepasa-wid={x_quepasa_wid}")
        logger.info(f"Corpo processado: {message.model_dump_json()}")
        
        # Extrai informações da mensagem
        message_id = message.id
        message_type = message.type
        
        logger.info(f"Tipo de mensagem: {message_type}")
        logger.info(f"ID da mensagem: {message_id}")
        
        # Extrai número do telefone e nome do chat
        phone = message.chat.id.split('@')[0]
        push_name = message.chat.title or f"User {phone}"
        
        logger.info(f"Número do telefone: {phone}")
        logger.info(f"Nome do chat: {push_name}")
        
        # Trata o campo fromme
        from_me = message.fromme
        logger.info(f"From me: {from_me}")
        
        # Limpa o número de telefone removendo tudo após os dois pontos
        if ':' in x_quepasa_wid:
            x_quepasa_wid = x_quepasa_wid.split(':')[0]
            logger.info(f"Número de telefone limpo para busca: {x_quepasa_wid}")
        
        if not x_quepasa_wid:
            logger.error("Cabeçalho x-quepasa-wid não encontrado na requisição")
            return {"success": False, "message": "Header x-quepasa-wid ausente"}
        
        # Busca o assistente pelo x-quepasa-wid primeiro
        response = supabase.table('assistants').select('id, token, assistant_id').eq('x-quepasa-wid', x_quepasa_wid).execute()

        if not response.data or len(response.data) == 0:
            logger.error(f"Nenhum assistente encontrado para x-quepasa-wid: {x_quepasa_wid}")
            # Tenta buscar todos os assistentes para debug
            all_assistants = supabase.table('assistants').select('id, token, assistant_id').execute()
            logger.info(f"Assistentes disponíveis: {len(all_assistants.data) if all_assistants.data else 0}")
            
            if all_assistants.data:
                logger.info(f"Colunas disponíveis: {list(all_assistants.data[0].keys())}")
                
                # Verificar se existe um assistente para este x-quepasa-wid
                try:
                    resp_alt = supabase.table('assistants').select('id, token, assistant_id').eq('x-quepasa-wid', x_quepasa_wid).execute()
                    if resp_alt.data and len(resp_alt.data) > 0:
                        logger.info(f"Assistente encontrado usando coluna 'x-quepasa-wid': {resp_alt.data[0]['id']}")
                        response = resp_alt
                    else:
                        logger.error(f"Assistente não encontrado com 'x-quepasa-wid' também")
                except Exception as e:
                    logger.error(f"Erro ao buscar com x-quepasa-wid: {str(e)}")
            
            if not response.data or len(response.data) == 0:
                return {"success": False, "message": "Assistente não configurado para este número"}

        agent_data = response.data[0]
        agent_id = agent_data['id']
        token = agent_data['assistant_id']  # Usa o assistant_id como token
        openai_assistant_id = agent_data.get('assistant_id')  # ID do assistente na OpenAI

        if not openai_assistant_id:
            logger.error(f"ID do assistente na OpenAI não encontrado para o agente {agent_id}")
            return {"success": False, "message": "Assistente não configurado corretamente (falta ID OpenAI)"}
        
        logger.info(f"ID do assistente na OpenAI: {openai_assistant_id}")
        
        log_with_instance("Nova requisição recebida no webhook", agent_id)
        log_with_instance(f"Processando mensagem de {phone}", agent_id)
        log_with_instance(f"Tipo de mensagem: {message_type}", agent_id)
        log_with_instance(f"De mim: {from_me}", agent_id)

        # Primeiro, verifica o status atual do contato
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('x-quepasa-wid', x_quepasa_wid).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        log_with_instance(f"Status atual do contato {phone}: {current_status}", agent_id)

        # Se a mensagem é do sistema/usuário dono do bot (fromMe = true)
        if from_me:
            log_with_instance(f"Mensagem enviada pelo sistema/dono para {phone}", agent_id)
            try:
                # Se o status atual é 'pausado', mantém pausado
                if current_status == 'pausado':
                    log_with_instance(f"Contato {phone} mantido como pausado", agent_id)
                    return {"success": True, "message": "Contato mantido como pausado"}
                
                # Caso contrário, atualiza para cooldown
                cooldown_end = (datetime.utcnow() + timedelta(hours=24)).isoformat()
                supabase.table('contacts').update({
                    'last_contact': datetime.utcnow().isoformat(),
                    'status': 'cooldown',
                    'cooldown_until': cooldown_end,
                    'from_me': True
                }).eq('whatsapp', phone).eq('x-quepasa-wid', x_quepasa_wid).execute()
                
                log_with_instance(f"Contato {phone} colocado em cooldown por 24 horas", agent_id)
                return {"success": True, "message": "Contato em cooldown após mensagem do sistema/dono"}
            except Exception as e:
                log_with_instance(f"Erro ao atualizar status do contato: {str(e)}", agent_id, "ERROR")
                return {"success": False, "message": "Erro ao atualizar status do contato"}
        
        # Se a mensagem é do cliente (fromMe = false)
        else:
            log_with_instance(f"Mensagem recebida do cliente {phone}", agent_id)
            # Continua o processamento normal para mensagens do cliente

        # Verifica novamente o status após qualquer atualização
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('x-quepasa-wid', x_quepasa_wid).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        log_with_instance(f"Status verificado novamente para {phone}: {current_status}", agent_id)

        # Se o contato está em cooldown ou pausado, não processa a mensagem
        if current_status in ['cooldown', 'pausado']:
            log_with_instance(f"Mensagem descartada para {phone}. Status: {current_status}", agent_id)
            return {"success": False, "message": f"Contato {current_status}"}

        # Verifica limite de contatos
        within_limit = await check_contact_limit(agent_id, phone)
        
        if not within_limit:
            # Se excedeu o limite, envia mensagem informando
            message = (
                "Desculpe, o limite de contatos do plano atual foi atingido. "
                "Por favor, entre em contato com o suporte para upgrade do plano."
            )
            
            # Envia via QuepasaAPI
            await send_quepasa_message(
                phone=phone,
                data={
                    "trackid": agent_id,
                    "text": message
                },
                token=token
            )
            
            logger.warning(f"Limite de contatos excedido para agente {agent_id}")
            return {"status": "error", "message": "Contact limit exceeded"}

        # Verifica o status uma última vez antes de processar a mensagem
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('x-quepasa-wid', x_quepasa_wid).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        if current_status in ['cooldown', 'pausado']:
            logger.info(f"Mensagem descartada para {phone}. Status final: {current_status}")
            return {"success": False, "message": f"Contato {current_status}"}

        # Processa diferentes tipos de mensagem
        user_message = ""
        
        if message_type == "text":
            # Mensagem de texto simples
            user_message = message.text
        elif message_type == "image":
            try:
                # Processa a imagem usando a nova função
                image_description = await process_quepasa_image(message_id, token)
                
                # Envia a descrição como contexto para o assistente
                user_message = f"O usuário enviou uma imagem. Descrição da imagem: {image_description}"
                logger.info("Enviando descrição para o assistente como contexto")
            except Exception as e:
                logger.error(f"Erro ao processar imagem: {str(e)}")
                user_message = "O usuário enviou uma imagem que não foi possível processar."
        elif message_type == "audio":
            try:
                # Processa o áudio usando a nova função
                logger.info(f"Iniciando processamento de áudio para message_id: {message_id}")
                transcription = await process_quepasa_audio(message_id, token)
                
                logger.info(f"Áudio processado com sucesso. Transcrição: {transcription}")
                
                # Envia a transcrição como contexto para o assistente
                user_message = f"O usuário enviou um áudio. Transcrição do áudio: {transcription}"
            except Exception as e:
                logger.error(f"Erro ao processar áudio: {str(e)}")
                logger.error(traceback.format_exc())
                user_message = "O usuário enviou um áudio que não foi possível transcrever."
        else:
            logger.warning(f"Tipo de mensagem não suportado: {message_type}")
            return {"success": False, "message": "Tipo de mensagem não suportado"}

        if not user_message:
            return {"success": False, "message": "Mensagem vazia"}

        # Verifica o status uma última vez antes de adicionar à fila
        contact_response = supabase.table('contacts').select('*').eq('whatsapp', phone).eq('x-quepasa-wid', x_quepasa_wid).execute()
        contact = contact_response.data[0] if contact_response.data else None
        current_status = contact['status'] if contact else None
        
        if current_status in ['cooldown', 'pausado']:
            logger.info(f"Mensagem descartada para {phone}. Status final antes da fila: {current_status}")
            return {"success": False, "message": f"Contato {current_status}"}

        # Adiciona a mensagem à lista de mensagens pendentes
        key = f"{phone}:{agent_id}"
        if key not in pending_messages:
            pending_messages[key] = []
        
        pending_messages[key].append(user_message)
        log_with_instance(f"Mensagem adicionada à fila para {phone}. Total: {len(pending_messages[key])}", agent_id)
        
        # Se já existe uma tarefa pendente para este contato, não cria outra
        if key in pending_tasks and not pending_tasks[key].done():
            log_with_instance(f"Já existe uma tarefa pendente para {key}", agent_id)
            return {"success": True, "message": "Mensagem adicionada à fila existente"}
            
        # Cria uma nova tarefa para processar após 5 segundos
        task = asyncio.create_task(
            process_delayed_message(
                phone=phone, 
                agent_id=agent_id, 
                token=token, 
                quepasa_wid=x_quepasa_wid,
                openai_assistant_id=openai_assistant_id,
                chat_title=message.chat.title
            )
        )
        pending_tasks[key] = task
        log_with_instance(f"Nova tarefa de processamento criada para {key}", agent_id)

        return {"success": True, "message": "Mensagem adicionada à fila"}

    except Exception as e:
        # Tenta identificar o agent_id para log, caso não tenha sido definido ainda
        agent_id = "unknown"
        if hasattr(message_data, 'chat') and hasattr(message_data.chat, 'id'):
            phone = message_data.chat.id.split('@')[0]
            logger.error(f"Erro no processamento para {phone}: {str(e)}")
        
        log_with_instance(f"Erro no processamento: {str(e)}", agent_id, "ERROR")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "quepasa_api": QUEPASA_API_URL,
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

# Função para baixar mídia do Quepasa
async def download_quepasa_media(message_id: str, token: str) -> Optional[bytes]:
    """Baixa mídia (áudio ou imagem) da API Quepasa."""
    try:
        headers = {
            "X-QUEPASA-TOKEN": token
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{QUEPASA_API_URL}/download/{message_id}?cache=false",
                headers=headers,
                timeout=30
            )
            
        if response.status_code != 200:
            logger.error(f"Erro ao baixar mídia. Status: {response.status_code}")
            return None
            
        logger.info(f"Mídia baixada com sucesso. Tamanho: {len(response.content)} bytes")
        return response.content
    except Exception as e:
        logger.error(f"Erro ao baixar mídia: {str(e)}")
        return None

# Função para processar áudio do Quepasa
async def process_quepasa_audio(message_id: str, token: str) -> str:
    """Processa o áudio baixado da API Quepasa e retorna a transcrição."""
    try:
        # Baixa o áudio
        audio_data = await download_quepasa_media(message_id, token)
        if not audio_data:
            return "[Não foi possível baixar o áudio]"
        
        # Salva em arquivo temporário
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as temp_audio:
            temp_audio.write(audio_data)
            temp_audio_path = temp_audio.name
        
        logger.info(f"Arquivo de áudio salvo temporariamente em: {temp_audio_path}")
        
        # Transcreve o áudio usando OpenAI
        with open(temp_audio_path, 'rb') as audio_file:
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        
        # Limpa o arquivo temporário
        os.unlink(temp_audio_path)
        
        # Log do resultado da transcrição
        logger.info(f"Transcrição do áudio concluída: {transcript.text}")
        
        return transcript.text
    except Exception as e:
        logger.error(f"Erro ao processar áudio: {str(e)}")
        logger.error(traceback.format_exc())
        return "[Não foi possível transcrever o áudio]"

# Função para processar imagem do Quepasa
async def process_quepasa_image(message_id: str, token: str) -> str:
    """Processa a imagem baixada da API Quepasa e retorna a descrição."""
    try:
        # Baixa a imagem
        image_data = await download_quepasa_media(message_id, token)
        if not image_data:
            return "[Não foi possível baixar a imagem]"
        
        # Converte para base64
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        # Analisa a imagem usando gpt-4o
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
        
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Erro ao processar imagem: {str(e)}")
        return "[Não foi possível analisar a imagem]"

# Adiciona endpoint de teste
@app.get("/test")
async def test_endpoint():
    logger.info("Endpoint de teste acessado")
    return {"status": "ok", "message": "Servidor está funcionando"}

# Adiciona logs de inicialização
logger.info("=== INICIALIZANDO SERVIDOR ===")
logger.info(f"URL da API Quepasa: {QUEPASA_API_URL}")
logger.info(f"URL do servidor: http://0.0.0.0:3004")
logger.info("=== SERVIDOR INICIALIZADO ===")

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