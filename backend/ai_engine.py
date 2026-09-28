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


async def _call_llm_endpoint(url: str, api_key: str, model: str, messages: List[Dict[str, str]]) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with httpx.AsyncClient(timeout=22.0) as client:
        resp = await client.post(
            url,
            headers=headers,
            json={"model": model, "messages": messages, "temperature": 0.75, "max_tokens": 220}
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if content:
                return content
    return ""


async def generate_ai_reply(state: Dict[str, Any], history: List[Dict[str, str]], user_message: str) -> Dict[str, Any]:
    """
    Gera a resposta da persona usando:
    1. API configurada pelo usuário (auto-detecta DeepSeek sk-32hex, OpenAI sk-proj, Groq gsk_)
    2. Fallback LLM gratuito (OpenAI-compatible via Pollinations) caso a chave esteja sem saldo/inválida
    3. Motor contextual de segurança (nunca repete respostas e responde nome, idade, cidade, FAQ e ofertas)
    """
    system_prompt = build_system_prompt(state)
    api_key = state.get("account", {}).get("ai_api_key", "").strip()
    provider = state.get("account", {}).get("ai_provider", "openai").lower()

    persona = state.get("persona", {})
    business = state.get("business", {})
    style = state.get("style", {})
    offers = [o for o in business.get("offers", []) if o.get("visible", True)]
    faqs = business.get("faqs", [])
    medias = [m for m in state.get("medias", []) if m.get("active", True)]

    msg_lower = user_message.lower().strip()

    # Verifica sinais de Mídias da Conversa
    triggered_media = None
    for m in medias:
        signals = [s.strip().lower() for s in m.get("signals", "").split(",") if s.strip()]
        if (signals and any(sig in msg_lower for sig in signals)) or any(w in msg_lower for w in ["foto", "vídeo", "video", "prévia", "previa", "amostra", "ver você", "ver sua"]):
            triggered_media = m
            break

    # Verifica se é pergunta de idade ("quantos anos", "sua idade") para NÃO confundir com "quanto custa"
    is_age_question = any(w in msg_lower for w in ["quantos anos", "sua idade", "que idade", "idade vc tem", "idade você tem"])

    # Verifica intenção real de preço / planos / comprar
    wants_offer = (not is_age_question) and any(
        w in msg_lower for w in [
            "preço", "preco", "valor", "quanto custa", "quanto ta", "quanto tá", "quanto é",
            "plano", "vip", "assinar", "entrar", "quero ver", "pix", "como funciona", "comprar", "acesso"
        ]
    )

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-12:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if content and not content.startswith("[Downsell]"):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    reply_text = ""

    # 1. Tenta a chave configurada pelo usuário (auto-detectando DeepSeek vs OpenAI vs Groq)
    if api_key:
        endpoints_to_try = []
        if api_key.startswith("gsk_") or provider == "groq":
            endpoints_to_try.append(("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"))
        elif provider == "deepseek" or (api_key.startswith("sk-") and len(api_key) <= 40):
            # Chaves sk- de 32 caracteres hexadecimais são DeepSeek, mas tentamos ambos
            endpoints_to_try.append(("https://api.deepseek.com/chat/completions", "deepseek-chat"))
            endpoints_to_try.append(("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"))
        else:
            endpoints_to_try.append(("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"))
            endpoints_to_try.append(("https://api.deepseek.com/chat/completions", "deepseek-chat"))

        for url, model in endpoints_to_try:
            try:
                reply_text = await _call_llm_endpoint(url, api_key, model, messages)
                if reply_text:
                    break
            except Exception as e:
                print(f"Aviso tentativa LLM ({url}): {e}")

    # 2. Se a chave do usuário estava sem saldo (402) ou inválida (401), usa LLM real gratuito compatível com OpenAI
    if not reply_text:
        try:
            reply_text = await _call_llm_endpoint("https://text.pollinations.ai/openai", "", "openai", messages)
        except Exception as e:
            print(f"Aviso fallback LLM Pollinations: {e}")

    if reply_text:
        for m in medias:
            tag = f"[SEND_MEDIA:{m.get('id')}]"
            if tag in reply_text:
                reply_text = reply_text.replace(tag, "").strip()
                triggered_media = m
        result: Dict[str, Any] = {
            "reply": reply_text,
            "tokens_used": 180
        }
        if triggered_media:
            result["media"] = triggered_media
        if (wants_offer or "r$" in reply_text.lower() or "plano" in reply_text.lower()) and offers:
            result["offer_card"] = offers[0]
        return result

    # 3. Fallback contextual inteligente (caso todas as redes externas oscilem)
    name = persona.get("name") or "Mari"
    age = persona.get("age") or 18
    city = persona.get("city") or "São Paulo"

    for faq in faqs:
        q = faq.get("question", "").lower()
        a = faq.get("answer", "")
        if q and a and any(word in msg_lower for word in q.split() if len(word) > 3):
            return {"reply": a, "tokens_used": 95}

    if is_age_question:
        return {
            "reply": f"tenho {age} aninhos rs 💜 e você, tem quantos?",
            "tokens_used": 80
        }

    if any(w in msg_lower for w in ["qual seu nome", "como vc chama", "como você se chama", "quem é você", "quem e vc", "seu nome"]):
        return {
            "reply": f"me chamo {name} 💜 prazer! e você, como gosta que eu te chame aqui?",
            "tokens_used": 85
        }

    if any(w in msg_lower for w in ["onde você mora", "onde vc mora", "de onde você é", "de onde vc e", "qual cidade", "sua cidade"]):
        return {
            "reply": f"sou de {city} 💜 e você, fala de onde?",
            "tokens_used": 85
        }

    if any(w in msg_lower for w in ["caro", "desconto", "menos", "barato", "não tenho", "nao tenho", "pix de"]):
        bargain = business.get("bargain", {})
        if bargain.get("enabled"):
            min_p = float(bargain.get("min_price") or 14.90)
            return {
                "reply": f"poxa, quero muito te ver lá no meu VIP... consigo fazer uma condição especial de R$ {min_p:.2f} pra você entrar agora no Pix, topa? 💜",
                "offer_card": {"name": offers[0]["name"] if offers else "VIP Exclusivo", "price": min_p},
                "tokens_used": 120
            }

    if wants_offer and offers:
        main_offer = offers[0]
        price_fmt = f"R$ {float(main_offer.get('price', 19)):.2f}".replace(".", ",")
        desc = main_offer.get("description") or "acesso imediato aos meus conteúdos exclusivos no Telegram"
        return {
            "reply": f"no meu VIP você tem {desc} 😍\n\no plano {main_offer.get('emoji', '💎')} {main_offer.get('name', 'Nova oferta')} tá saindo por {price_fmt} ({main_offer.get('duration', 30)} dias). Quer que eu gere o botão seguro pra você entrar agora?",
            "offer_card": main_offer,
            "tokens_used": 145
        }

    if triggered_media:
        return {
            "reply": "separei isso aqui pra você ver um pouquinho do meu jeito... 😈 me diz o que achou!",
            "media": triggered_media,
            "tokens_used": 110
        }

    # Variedade de respostas naturais sem repetição
    prev_assistant_msgs = {h.get("content", "") for h in history if h.get("role") == "assistant"}
    candidates = [
        f"ai sim 💜 gosto de conversar assim no privado rs... você já conhece meu VIP ou é sua primeira vez por aqui?",
        f"me conta, o que você mais gosta de ver quando entra num VIP privado? 😈",
        f"tô online aqui agora rs... se quiser dar uma olhadinha nos meus planos do VIP é só me falar que te mostro 💜",
        f"gostei de você rs 👀 lá no meu VIP eu posto tudo sem censura, quer que eu te explique como funciona o acesso?"
    ]
    for c in candidates:
        if c not in prev_assistant_msgs:
            return {"reply": c, "tokens_used": 110}

    return {"reply": candidates[0], "tokens_used": 110}
