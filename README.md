# WhatsApp GPT Bot

Um sistema de integração entre WhatsApp e OpenAI para automatizar atendimentos via Evolution API.

## Recursos

- Integração com OpenAI GPT usando a API oficial
- Processamento de mensagens em tempo real enviadas pelo WhatsApp
- Suporte a imagens (com descrição automática)
- Suporte a áudio (com transcrição automática)
- Chamada de funções personalizadas (notificar, funil_de_vendas, etc.)
- Integração com Supabase para armazenamento de dados
- Limitação de contatos por plano

## Requisitos

- Docker e Docker Compose
- API Key da OpenAI
- Conta na Supabase
- Evolution API configurada

## Variáveis de Ambiente

O sistema utiliza as seguintes variáveis de ambiente que devem ser configuradas no Portainer:

```
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxx
EVOLUTION_API_URL=https://sua-evolution-api.com
EVOLUTION_API_KEY=sua-chave-api
SUPABASE_URL=https://seu-projeto.supabase.co
SUPABASE_KEY=sua-chave-supabase
```

## Instalação e Execução

### Usando Docker

```bash
# Clonar o repositório
git clone https://github.com/seu-usuario/whatsapp-gpt-bot.git
cd whatsapp-gpt-bot

# Construir a imagem Docker
docker build -t whatsapp-gpt .

# Executar o contêiner
docker run -d -p 3004:3004 \
  -e OPENAI_API_KEY=sua-chave \
  -e EVOLUTION_API_URL=url-da-api \
  -e EVOLUTION_API_KEY=chave-da-api \
  -e SUPABASE_URL=url-do-supabase \
  -e SUPABASE_KEY=chave-do-supabase \
  --name whatsapp-gpt whatsapp-gpt
```

### Usando Docker Compose

```bash
# Editar o arquivo docker-compose.yml com suas variáveis
nano docker-compose.yml

# Executar com Docker Compose
docker-compose up -d
```

### Usando Portainer

1. Acesse seu Portainer
2. Vá para "Stacks" e clique em "Add stack"
3. Faça upload do arquivo docker-compose.yml ou cole seu conteúdo
4. Configure as variáveis de ambiente necessárias
5. Implante o stack

## Configuração do Webhook

Após iniciar o serviço, configure o webhook no Evolution API para apontar para:

```
http://seu-servidor:3004/webhook
```

## Estrutura do Projeto

```
whatsapp-gpt-bot/
├── src/                # Código fonte
│   ├── main.py         # Arquivo principal da aplicação
├── Dockerfile          # Configuração do Docker
├── docker-compose.yml  # Configuração do Docker Compose
├── requirements.txt    # Dependências Python
├── .dockerignore       # Arquivos ignorados pelo Docker
└── README.md           # Este arquivo
```

## Uso

O sistema processará automaticamente mensagens recebidas pelo WhatsApp através da Evolution API, utilizando a OpenAI para gerar respostas adequadas.

## Funcionalidades Principais

- Processamento de texto, imagens e áudio
- Divisão inteligente de mensagens longas
- Reconhecimento de formatação e emoji
- Integração com Supabase para persistência
- Sistema de limitação de contatos por plano
- Funções personalizáveis via webhooks

## Licença

Este projeto está licenciado sob a Licença MIT. 