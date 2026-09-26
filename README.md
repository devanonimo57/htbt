# Guia de Inicialização e Uso: HotBot AI

Parabéns! O seu sistema proprietário **HotBot AI** agora conta com toda a arquitetura funcional do Sharkbot.

---

## 1. O que foi construído

1. **Dashboard Web Completo (`frontend/public/index.html`):**
   * **Resumo do Atendimento:** Checklist guiado dos 5 passos essenciais.
   * **Estratégias Prontas:** 3 templates de funil (*Conexão que Converte*, *Venda Rápida* e *Experiência Premium*) que reconfiguram a IA em 1 clique.
   * **Persona & Identidade:** Configuração de nome, idade, cidade, gênero gramatical, biografia oculta e traços sensuais.
   * **Negócio & Ofertas:** Cadastro de planos, preços, dias de duração e barganha inteligente com preço de piso.
   * **Jeito de Conversar:** Ajuste fino de agressividade de oferta, cadência de digitação humana, uso de gírias e regras inegociáveis.
   * **Mídias da Conversa:** Amostras com gatilhos semânticos (ex: *manda foto, biquíni, corpo*) e modo spoiler nativo.
   * **Downsell:** Régua de recuperação por tempo de silêncio (5m, 1h) com cupom relâmpago.
   * **Upsell:** Pós-venda imediata para vender pacotes extras de vídeos assim que o Pix é pago.
   * **Memória & CRM:** Acompanhamento de leads por etapas (*Em conversa*, *Pix Gerado*, *Pix Pago*).
   * **Simulador Lateral em Tempo Real:** Teste ao vivo da conversa com a modelo antes de subir para o Telegram.

2. **Backend & Banco de Dados Persistente (`backend/main.py`, `models.py`):**
   * Banco relacional SQLite assíncrono (`hotbot.db`).
   * API FastAPI completa para salvar configurações e alimentar o chat.
   * Webhook pronto para a **Pushin Pay**.

3. **Automação do Telegram & Canal VIP (`backend/telegram_service.py`):**
   * Bot em `aiogram 3.x`.
   * Geração automática de link de convite exclusivo de uso único (`create_chat_invite_link`).
   * Entrega imediata pós-pagamento.

---

## 2. Como Rodar no seu Computador

### Passo 1: Abrir o Painel Visual
Basta abrir o arquivo no seu navegador (Google Chrome, Edge, etc.):
👉 `C:\Users\Yago\.gemini\antigravity\scratch\hotbot-ai\frontend\public\index.html`

### Passo 2: Iniciar o Servidor Backend (Opcional para ligar o Telegram)
Abra o terminal na pasta `backend`:
```powershell
cd C:\Users\Yago\.gemini\antigravity\scratch\hotbot-ai\backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

---

## 3. Como colocar no Ar no Telegram

1. Acesse o `@BotFather` no Telegram e crie seu bot para pegar o **Token**.
2. Crie o seu **Canal VIP Privado** no Telegram e adicione o seu bot como **Administrador** (com permissão de *Convidar Usuários via Link*).
3. Pegue seu **Token da Pushin Pay** no painel da sua conta Pushin Pay.
4. No painel do **HotBot AI** (na aba *Publicar no Bot*), cole esses dados e clique em **Salvar alterações**.
