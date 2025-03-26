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
load_dotenv()

# Configurações
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "https://evo.ganchodigital.com.br")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

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
        assistant_data = supabase.table('assistants').select(
            'user_id'
        ).eq('instance_name', instance_name).execute()
        
        if not assistant_data.data or len(assistant_data.data) == 0:
            logger.error(f"Assistente não encontrado: {instance_name}")
            return True  # Permite continuar se não encontrar o assistente
        
        user_id = assistant_data.data[0]['user_id']
        logger.info(f"ID do usuário encontrado: {user_id}")
        
        # Busca o plano do usuário - usando coluna 'id' em vez de 'user_id'
        user_data = supabase.table('users').select(
            'plan'
        ).eq('id', user_id).execute()
        
        if not user_data.data or len(user_data.data) == 0:
            logger.error(f"Usuário não encontrado com id: {user_id}")
            return True  # Se não encontrar o usuário, permite continuar
        
        user_plan = user_data.data[0]['plan']
        
        # Define limite baseado no plano
        plan_limits = {
            'starter': 100,
            'essential': 500,
            'agent': 2000,
            'empresa': 10000
        }
        
        contact_limit = plan_limits.get(user_plan.lower() if isinstance(user_plan, str) else 'starter', 100)
        
        # Calcula data de 30 dias atrás
        thirty_days_ago = datetime.now() - timedelta(days=30)
        
        # Conta contatos do usuário nos últimos 30 dias
        try:
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
                if response.status_code != 200:
                    logger.error(f"Erro ao enviar mensagem: {response.text}")
                    
            return True
        
        # Padrão para identificar emojis
        emoji_pattern = r'[\U0001F000-\U0001F9FF]|[\u2600-\u26FF\u2700-\u27BF]'
        
        # Substitui URLs por marcadores temporários para preservá-las durante a divisão
        urls = []
        def capture_url(match):
            urls.append(match.group(0))
            return f"[[URL{len(urls)-1}]]"
        
        # Captura URLs e substitui por marcadores
        formatted_message = re.sub(r'(https?://[^\s]+)', capture_url, response_text)
        
        # Tenta dividir a mensagem em frases completas primeiro
        sentences = re.split(r'(?<=[.!?])\s+', formatted_message)
        
        # Se houver muitas frases curtas, agrupa-as
        merged_sentences = []
        current_merge = ""
        
        for sentence in sentences:
            # Se a frase atual + a próxima forem menores que 150 caracteres, as junta
            if len(current_merge + " " + sentence) < 150:
                if current_merge:
                    current_merge += " " + sentence
                else:
                    current_merge = sentence
            else:
                # A adição desta frase tornaria o grupo grande demais, então finalizamos o grupo atual
                if current_merge:
                    merged_sentences.append(current_merge)
                current_merge = sentence
                
        # Adiciona o último grupo se houver
        if current_merge:
            merged_sentences.append(current_merge)
            
        # Se não conseguimos dividir bem por frases, dividimos usando o método anterior
        if len(merged_sentences) <= 1:
            # Divide o texto em palavras mantendo a pontuação
            words = re.findall(r'\S+|\s+', formatted_message)
            
            # Divide a mensagem em partes
            parts = []
            current_part = ""
            last_was_punctuation_or_emoji = False  # Para controlar pontuação seguida de emoji
            
            for i, word in enumerate(words):
                current_part += word
                
                # Verifica se a palavra atual contém emoji
                has_emoji = bool(re.search(emoji_pattern, word))
                # Verifica se a próxima palavra (se existir) contém emoji
                next_has_emoji = bool(re.search(emoji_pattern, words[i+1])) if i < len(words)-1 else False
                
                # Verifica se a palavra atual ou palavras anteriores formam um padrão de número seguido de ponto
                is_numbered_list_item = False
                if word.strip().endswith('.'):
                    # Verificar se esta palavra é um número seguido de ponto
                    if re.match(r'^\d+\.$', word.strip()):
                        is_numbered_list_item = True
                    # Verificar se a palavra anterior + esta palavra forma um "X.Y." (ex: "1.1.")
                    elif i > 0 and re.match(r'^\.\d+\.$', word.strip()) and words[i-1].strip().endswith(r'\d'):
                        is_numbered_list_item = True
                
                # Verifica se termina com ? ou !
                ends_with_punct = (
                    word.strip().endswith(('?', '!')) or 
                    (word.strip().endswith('.') and not is_numbered_list_item)
                )
                
                # Se a palavra tem pontuação e a próxima tem emoji, não quebra a mensagem
                if ends_with_punct and next_has_emoji:
                    last_was_punctuation_or_emoji = True
                    continue
                    
                # Condições para quebra de mensagem
                should_break = (
                    # Se tem emoji e a próxima palavra não tem emoji e não estamos continuando após uma pontuação
                    (has_emoji and not next_has_emoji and not last_was_punctuation_or_emoji) or
                    # Se termina com pontuação (exceto número seguido de ponto) e não tem emoji na próxima palavra
                    (ends_with_punct and not next_has_emoji)
                ) and len(current_part.strip()) >= 80  # Parte deve ter pelo menos 80 caracteres
                
                # Reseta o flag se esta palavra não tem emoji
                if not has_emoji:
                    last_was_punctuation_or_emoji = False
                
                if should_break and current_part.strip():
                    # Reinsere as URLs
                    with_urls = re.sub(r'\[\[URL(\d+)\]\]', lambda m: urls[int(m.group(1))], current_part)
                    
                    # Filtra asteriscos em vez de removê-los completamente
                    # Converte **texto** para *texto*
                    final_part = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', with_urls)
                    
                    parts.append(final_part.strip())
                    current_part = ""
            
            # Adiciona a última parte se houver conteúdo
            if current_part.strip():
                # Reinsere as URLs
                with_urls = re.sub(r'\[\[URL(\d+)\]\]', lambda m: urls[int(m.group(1))], current_part)
                
                # Filtra asteriscos em vez de removê-los completamente
                # Converte **texto** para *texto*
                final_part = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', with_urls)
                
                parts.append(final_part.strip())
            
            # Se não tiver partes identificáveis, usa a mensagem original como uma única mensagem
            if not parts:
                parts = [response_text]
            
            # Novo: Verifica se temos muitas partes pequenas e tenta agrupá-las se necessário
            if len(parts) > 2:
                grouped_parts = []
                current_group = ""
                
                for part in parts:
                    if len(current_group + " " + part) < 150:
                        if current_group:
                            current_group += " " + part
                        else:
                            current_group = part
                    else:
                        if current_group:
                            grouped_parts.append(current_group)
                        current_group = part
                
                # Adiciona o último grupo
                if current_group:
                    grouped_parts.append(current_group)
                
                parts = grouped_parts
        else:
            # Usar as frases mescladas como partes
            parts = merged_sentences
        
        # Log das partes para debug
        logger.info(f"Mensagem dividida em {len(parts)} partes:")
        for i, part in enumerate(parts):
            logger.info(f"Parte {i+1}: {part}")
        
        # Envia cada parte como uma mensagem separada
        async with httpx.AsyncClient() as client:
            for i, message in enumerate(parts):
                # Filtra asteriscos duplos antes de enviar
                message = re.sub(r'\*\*([^*]+)\*\*', r'*\1*', message)
                
                logger.info(f"Enviando parte {i+1}/{len(parts)}: {message}")
                
                evolution_url = f"{server_url}/message/sendText/{instance_name}"
                
                headers = {
                    "Content-Type": "application/json",
                    "apikey": apikey
                }
                
                # Payload no formato correto
                payload = {
                    "number": phone,
                    "text": message,
                    "delay": 1200
                }

                logger.info(f"Fazendo requisição para: {evolution_url} com payload: {payload}")
                response = await client.post(
                    evolution_url,
                    headers=headers,
                    json=payload
                )
                
                logger.info(f"Resposta da requisição {i+1}: Status {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"Erro ao enviar mensagem: {response.text}")
                
                # Pausa maior entre mensagens para garantir a ordem
                if i < len(parts) - 1:
                    await asyncio.sleep(1.0)
                
            return True
                
    except Exception as e:
        logger.error(f"Erro ao enviar mensagens: {str(e)}")
        traceback.print_exc()  # Adiciona rastreamento completo do erro
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
    uvicorn.run("main:app", host="0.0.0.0", port=3004, reload=True) 