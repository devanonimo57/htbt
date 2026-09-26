import httpx
import json
from typing import Dict, Any, List

def build_system_prompt(state: Dict[str, Any]) -> str:
    """
    Constrói o System Prompt completo da Persona com todos os parâmetros do Sharkbot:
    - Persona e identidade (nome, idade, apresentação/gênero, cidade, biografia curta, traços)
    - Negócio e ofertas (contexto da criadora, ofertas visíveis, negociação de preço, dúvidas frequentes)
    - Jeito de conversar (estratégia comercial, nível de flerte, gírias, cadência, espelhamento, tom, tamanho, emojis, objetivo, regras próprias)
    - Mídias da conversa (arquivos, descrição, objetivo principal, sinais da conversa)
    """
    persona = state.get("persona", {})
    business = state.get("business", {})
    style = state.get("style", {})
    medias = state.get("medias", [])

    offers = business.get("offers", [])
    visible_offers = [o for o in offers if o.get("visible", True)]
    offers_text = "\n".join([
        f"- {o.get('emoji', '💎')} {o.get('name', 'Nova oferta')}: R$ {float(o.get('price', 19)):.2f} | Duração: {o.get('duration', 30)} dias | Destaque: {'Sim' if o.get('highlight') else 'Não'} | Detalhes: {o.get('description', '')}"
        for o in visible_offers
    ])

    faqs = business.get("faqs", [])
    faqs_text = "\n".join([
        f"- P: {f.get('question', '')}\n  R: {f.get('answer', '')}"
        for f in faqs if f.get("question")
    ])

    active_medias = [m for m in medias if m.get("active", True)]
    medias_text = "\n".join([
        f"- [MEDIA_ID:{m.get('id')}] Nome: {m.get('name')} | Tipo: {m.get('type')} | O que mostra: {m.get('description')} | Objetivo: {m.get('objective')} | Sinais: {m.get('signals')}"
        for m in active_medias
    ])

    bargain = business.get("bargain", {})
    bargain_rule = (
        f"ATIVADA (Limite mínimo autorizado: R$ {float(bargain.get('min_price', 14.90)):.2f})"
        if bargain.get("enabled") else "DESATIVADA (Preços estritamente fixos, não dê descontos)."
    )

    prompt = f"""Você é {persona.get('name', 'Atendimento')}, {persona.get('age', 18)} anos, morando em {persona.get('city', 'São Paulo')}.
Apresentação / Gênero: {persona.get('gender', 'Feminina')} ({persona.get('gender_desc', 'Fala e se apresenta no feminino.')}).
Biografia curta: {persona.get('bio', '')}
Traços de personalidade: {persona.get('traits', 'espontânea, atenta, bem-humorada')}

=== CONTEXTO DA CRIADORA E DO VIP ===
{business.get('context', '')}

=== OFERTAS DISPONÍVEIS ===
{offers_text if offers_text else 'Nenhuma oferta ativa.'}

=== NEGOCIAÇÃO DE PREÇO ===
{bargain_rule}

=== DÚVIDAS QUE VOCÊ JÁ SABE RESPONDER (FAQ) ===
{faqs_text if faqs_text else 'Responda com base no contexto.'}

=== JEITO DE CONVERSAR E HUMANIZAÇÃO ===
- Estratégia comercial: {style.get('strategy', 'Conexão primeiro')}
- Nível de flerte: {style.get('flirt', 'Leve')}
- Uso de gírias: {style.get('slang', 'Natural')}
- Cadência: {style.get('cadence', 'Natural')}
- Espelhar o jeito do visitante: {'Sim' if style.get('mirror_visitor', True) else 'Não'}
- Usar o nome do lead: {'Sim' if style.get('use_lead_name', False) else 'Não'}
- Tom de voz: {style.get('tone', 'Acolhedor')}
- Tamanho das respostas: {style.get('length', 'Curta')} (respostas curtas parecem chat real de celular; nunca mande listas robóticas)
- Uso de emojis: {style.get('emojis', 'Leve')}

=== OBJETIVO PRINCIPAL ===
{style.get('objective', 'Entender o interesse, lembrar preferências e conduzir naturalmente para o acesso mais adequado.')}

=== REGRAS PRÓPRIAS (OBRIGATÓRIAS) ===
{style.get('rules', '')}
- Palavras de encaminhamento para suporte: {style.get('handoff_words', 'suporte, atendente, problema no pagamento')}

=== MÍDIAS DA CONVERSA ===
Se a mensagem do visitante combinar com os sinais ou objetivo de alguma mídia abaixo, inclua a tag [SEND_MEDIA:ID] no final da sua resposta:
{medias_text if medias_text else 'Nenhuma mídia cadastrada.'}

IMPORTANTE: Converse sempre em primeira pessoa no chat privado do Telegram, sem parecer robô ou suporte técnico.
"""
    return prompt


async def generate_ai_reply(state: Dict[str, Any], history: List[Dict[str, str]], user_message: str) -> Dict[str, Any]:
    """
    Gera a resposta da persona usando API real (OpenAI / Gemini / Groq) se configurada,
    ou motor semântico local fiel às configurações do Sharkbot.
    """
    system_prompt = build_system_prompt(state)
    api_key = state.get("account", {}).get("ai_api_key", "").strip()
    provider = state.get("account", {}).get("ai_provider", "openai").lower()

    # Se o usuário cadastrou chave de API (OpenAI ou Groq compatível)
    if api_key and provider in ("openai", "groq"):
        base_url = "https://api.openai.com/v1/chat/completions" if provider == "openai" else "https://api.groq.com/openai/v1/chat/completions"
        model = "gpt-4o-mini" if provider == "openai" else "llama-3.3-70b-versatile"
        messages = [{"role": "system", "content": system_prompt}]
        for h in history[-10:]:
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": user_message})

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(
                    base_url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "temperature": 0.75, "max_tokens": 220}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    reply_text = data["choices"][0]["message"]["content"]
                    return {"reply": reply_text, "tokens_used": data.get("usage", {}).get("total_tokens", 180)}
        except Exception as e:
            print(f"Erro na chamada LLM: {e}")

    # Motor de resposta contextual inteligente integrado às configurações da tela
    persona = state.get("persona", {})
    business = state.get("business", {})
    style = state.get("style", {})
    offers = [o for o in business.get("offers", []) if o.get("visible", True)]
    faqs = business.get("faqs", [])
    medias = [m for m in state.get("medias", []) if m.get("active", True)]

    msg_lower = user_message.lower()

    # 1. Verifica se bate com alguma Dúvida Frequente (FAQ) cadastrada na aba Negócio e Ofertas
    for faq in faqs:
        q = faq.get("question", "").lower()
        a = faq.get("answer", "")
        if q and a and any(word in msg_lower for word in q.split() if len(word) > 3):
            return {"reply": a, "tokens_used": 95}

    # 2. Verifica sinais de Mídias da Conversa
    triggered_media = None
    for m in medias:
        signals = [s.strip().lower() for s in m.get("signals", "").split(",") if s.strip()]
        if any(sig in msg_lower for sig in signals) or any(w in msg_lower for w in ["foto", "vídeo", "video", "prévia", "previa", "amostra", "ver você"]):
            triggered_media = m
            break

    # 3. Verifica negociação / achou caro
    if any(w in msg_lower for w in ["caro", "desconto", "menos", "barato", "não tenho", "nao tenho", "pix de"]):
        bargain = business.get("bargain", {})
        if bargain.get("enabled"):
            min_p = float(bargain.get("min_price", 14.90))
            return {
                "reply": f"poxa, quero muito te ver lá no meu VIP... consigo fazer uma condição especial de R$ {min_p:.2f} pra você entrar agora no Pix, topa? 💜",
                "offer_card": {"name": offers[0]["name"] if offers else "VIP Exclusivo", "price": min_p},
                "tokens_used": 120
            }

    # 4. Verifica intenção de preço / planos / comprar
    if any(w in msg_lower for w in ["preço", "preco", "valor", "quanto", "plano", "vip", "assinar", "entrar", "quero", "pix", "como funciona"]):
        if offers:
            main_offer = offers[0]
            price_fmt = f"R$ {float(main_offer.get('price', 19)):.2f}".replace(".", ",")
            desc = main_offer.get("description") or "acesso imediato aos meus conteúdos exclusivos no Telegram"
            return {
                "reply": f"no meu VIP você tem {desc} 😍\n\no plano {main_offer.get('emoji', '💎')} {main_offer.get('name', 'Nova oferta')} tá saindo por {price_fmt} ({main_offer.get('duration', 30)} dias). Quer que eu gere o botão seguro pra você entrar agora?",
                "offer_card": main_offer,
                "tokens_used": 145
            }

    # 5. Se acionou mídia
    if triggered_media:
        return {
            "reply": f"separei isso aqui pra você ver um pouquinho do meu jeito... 😈 me diz o que achou!",
            "media": triggered_media,
            "tokens_used": 110
        }

    # 6. Resposta natural conforme estratégia e tom configurados
    strategy = style.get("strategy", "Conexão primeiro")
    city = persona.get("city", "São Paulo")
    if any(w in msg_lower for w in ["oi", "oii", "olá", "ola", "tudo bem", "opa", "bom dia", "boa tarde", "boa noite"]):
        if strategy == "Direto":
            main_offer = offers[0] if offers else {"name": "Acesso VIP", "price": 19.90}
            return {
                "reply": f"oii! tudo ótimo por aqui 💜 você já veio conhecer meu VIP privado ou quer tirar alguma dúvida antes de liberar seu acesso?",
                "tokens_used": 105
            }
        return {
            "reply": f"oii, tudo bem e com você? 💜 sou de {city} rs, me conta, o que te chamou atenção pra vir falar comigo aqui no privado?",
            "tokens_used": 110
        }

    return {
        "reply": f"adorei isso que você falou rs... lá no meu VIP privado eu fico bem mais à vontade e posto tudo sem censura. Você curte conteúdo mais provocante ou prefere vídeos mais longos?",
        "tokens_used": 130
    }
