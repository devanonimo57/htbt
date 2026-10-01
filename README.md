# HotBot AI — Plataforma Multi-Bot Telegram para Vendas e Acesso VIP

Sistema completo e autônomo de automação de vendas digitais e gestão de canais VIP no Telegram. Integra múltiplos bots operando de forma simultânea (Modo Fluxo e Modo IA), checkout Pix automatizado com Pushin Pay, réguas de recuperação por tempo (downsell/remarketing) e entrega atômica de acessos privados.

---

## Arquitetura & Stack Tecnológica

* **Backend:** Python 3.12+ / FastAPI / Uvicorn (Arquitetura assíncrona orientada a eventos)
* **Motor do Telegram:** `aiogram 3.x` com polling assíncrono particionado por instância de bot
* **Gateway de Pagamento:** Pushin Pay (Geração de Pix Copia e Cola, polling de verificação e webhooks de conciliação)
* **Persistência de Dados:** SQLite com Write-Ahead Logging (`sqlite+aiosqlite`), garantindo integridade transacional
* **Frontend:** Single Page Application (SPA) responsiva com editor de fluxos visuais, CRM de leads e telemetria financeira em tempo real
* **Hospedagem Recomendada:** Windows Server VPS / Linux VPS com serviço persistente

---

## Módulos Principais

### 1. Gerenciador Multi-Bot (NeurotBot)
* **Múltiplos Bots Simultâneos:** Suporta execução concorrente de múltiplos bots em um único processo backend.
* **Isolamento de Leads:** Cada lead é indexado e isolado logicamente pela chave composta `(bot_id, user_id)`, impedindo cruzamento de dados entre bots diferentes.
* **Modo Fluxo (Flow Mode):** Execução determinística baseada em botões inline interativos. **Consumo zero de IA externa**, eliminando latência de LLM e custos de API.
* **Modo IA (AI Mode):** Integração com modelos generativos para conversação dinâmica e vendas consultivas.

### 2. Motor de Pagamento e Entrega em Duas Fases
* **Fase 1 (Liquidação Financeira Atômica):**
  * Barreira de idempotência com trava `sales_counted` para evitar duplicidade de contagem de receita em retries ou webhooks concorrentes.
  * Sincronização atômica entre a tabela relacional `orders` e o registro de estado global.
* **Fase 2 (Despacho e Entrega com Garantia At-Least-Once):**
  * Geração dinâmica de link de convite de uso único (`create_chat_invite_link`) no canal VIP configurado.
  * Rota de reenvio manual e retry automático em caso de indisponibilidade momentânea da API do Telegram.

### 3. Réguas de Downsell & Remarketing
* **Recuperação por Inatividade:** Disparos automáticos programados para clientes que geraram Pix mas não concluíram o pagamento dentro da janela de conversão.
* **Despacho Seguro:** Mensagens de downsell são transmitidas estritamente pelo bot que originou a conversa, respeitando os limites de taxa (rate limits) da API do Telegram.

### 4. Segurança e Integridade Operacional
* **Autenticação Administrativa:** Proteção de endpoints de configuração via token (`.admin_token`).
* **Sanitização e Redação de Segredos:** Tokens de bots e chaves de API são mascarados automaticamente nas respostas HTTP (`REDACTED_SECRET_MASK`), impedindo vazamento de credenciais na interface.
* **Reconciliação Contábil Diária:** Cálculo de faturamento e vendas baseado em transações liquidadas no fuso horário oficial de Brasília (BRT).

---

## Estrutura do Repositório

```text
├── backend/
│   ├── main.py                  # Endpoints REST, ciclo de vida do FastAPI e persistência
│   ├── telegram_service.py      # Orquestrador do Telegram (aiogram 3.x), entrega e polling
│   ├── pushin_pay.py            # Integração com o gateway Pushin Pay e webhooks Pix
│   ├── models.py                # Esquema de dados relacional SQLAlchemy
│   ├── requirements.txt         # Dependências Python
│   └── tests/                   # Suíte de testes automatizados (43 testes unitários)
│       ├── test_stage1_security.py
│       ├── test_stage2_and_3.py
│       └── test_audit_round3.py
├── frontend/
│   └── public/
│       └── index.html           # Interface visual do painel administrativo
├── docker-compose.yml           # Definição de containerização e volumes persistentes
├── ATUALIZAR_REPOSITORIO.bat    # Script de sincronização com o GitHub
└── README.md
```

---

## Como Executar Localmente

### 1. Clonar e Configurar o Ambiente Virtual
```powershell
cd C:\Users\Yago\.gemini\antigravity\scratch\hotbot-ai\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Iniciar o Servidor Backend
```powershell
python -m uvicorn main:app --reload --port 8000
```
O painel administrativo estará acessível em: `http://localhost:8000/dashboard/`.

---

## Implantação em Produção (Windows VPS)

Para atualizar e iniciar o serviço na VPS Windows Server, configure seu Personal Access Token do GitHub e execute no PowerShell:

```powershell
$GITHUB_TOKEN = "<SEU_GITHUB_PERSONAL_ACCESS_TOKEN>"
cd C:\Users\xdatacenter
curl.exe -L -H "Authorization: token $GITHUB_TOKEN" -o htbt_new.zip "https://github.com/devanonimo57/htbt/archive/refs/heads/main.zip"
if ($LASTEXITCODE -eq 0) {
    taskkill /F /IM python.exe /T 2>$null
    Expand-Archive -Path htbt_new.zip -DestinationPath C:\Users\xdatacenter\app -Force
    Remove-Item -Recurse -Force C:\Users\xdatacenter\app\htbt-main\backend\__pycache__ -ErrorAction SilentlyContinue
    cd C:\Users\xdatacenter\app\htbt-main\backend
    if (!(Test-Path .admin_token)) {
        $genToken = [System.Guid]::NewGuid().ToString("N").Substring(0, 16)
        Set-Content -Path .admin_token -Value $genToken -NoNewline
    }
    Write-Host "`n=== SENHA DO PAINEL (ADMIN_TOKEN): $(Get-Content .admin_token) ===`n" -ForegroundColor Green
    python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
} else {
    Write-Host "`n[ERRO] Falha ao baixar pacote do GitHub. Servidor Python mantido intacto.`n" -ForegroundColor Red
}
```

---

## Verificação e Testes Automatizados

O sistema conta com suíte de testes cobrindo segurança, transações financeiras, concorrência e entrega:

```powershell
cd C:\Users\Yago\.gemini\antigravity\scratch\hotbot-ai
python -m unittest discover -s backend/tests -p "test_*.py"
```

* Resultado esperado: **43 testes aprovados (OK)** sem erros ou falhas.
