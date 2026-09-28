import asyncio
import re
import httpx
import json
from typing import Dict, Any, List, Optional


def _compute_effective_min_price(offers: List[Dict[str, Any]], bargain: Dict[str, Any]) -> float:
    base_price = float(offers[0].get("price", 19.0)) if offers else 19.0
    max_pct = float(bargain.get("max_discount_pct", 40) or 0)
    configured_min = float(bargain.get("min_price", 0) or 0)
    pct_floor = round(base_price * max(0.1, (100.0 - max_pct) / 100.0), 2)
    if configured_min > 0:
        return max(5.0, min(base_price, configured_min))
    return max(5.0, pct_floor)


def build_system_prompt(state: Dict[str, Any], lead_name: str = "") -> str:
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
        f"- {o.get('emoji', '💎')} {o.get('name', 'Nova oferta')}: R$ {float(o.get('price', 19)):.2f} | Duração: {o.get('duration', 30)} dias | Destaque: {'Sim' if o.get('highlight') else 'Não'} | O que entrega: {o.get('description', '')}"
        for o in visible_offers
    ])

    faqs = business.get("faqs", [])
    faqs_text = "\n".join([
        f"- P: {f.get('question', '')}\n  R: {f.get('answer', '')}"
        for f in faqs if f.get("question")
    ])

    active_medias = [m for m in medias if m.get("active", True)]
    medias_text = "\n".join([
        f"- [SEND_MEDIA:{m.get('id')}] Nome: {m.get('name')} | Tipo: {m.get('type')} | O que mostra: {m.get('description')} | Objetivo: {m.get('objective')} | Sinais: {m.get('signals')}"
        for m in active_medias
    ])

    bargain = business.get("bargain", {})
    eff_min = _compute_effective_min_price(visible_offers, bargain)
    if bargain.get("enabled"):
        bargain_rule = (
            f"ATIVADA (Desconto máximo: {bargain.get('max_discount_pct', 40)}% | "
            f"Preço mínimo autorizado: R$ {eff_min:.2f} | Validade da proposta: {bargain.get('validity_minutes', 30)} min). "
            f"Se o visitante achar caro, pedir desconto ou disser que tem pouco dinheiro, ofereça um valor promocional entre R$ {eff_min:.2f} e o valor original, "
            f"e inclua no final da resposta a tag [DISCOUNT_PRICE:VALOR] (ex: [DISCOUNT_PRICE:{eff_min:.2f}]) para atualizar o botão do PIX automaticamente."
        )
    else:
        bargain_rule = "DESATIVADA (Preços estritamente fixos, nunca dê descontos nem baixe o valor)."

    raw_name = (persona.get("name") or "").strip()
    if not raw_name or raw_name.lower() == "atendimento":
        name_instruction = "Apresente-se como a criadora dona do VIP (nunca diga 'meu nome é Atendimento'; se perguntarem seu nome, use um apelido feminino natural como Mari ou Mel)."
    else:
        name_instruction = f"Seu nome é {raw_name}. Sempre use esse nome quando perguntarem."

    city_val = (persona.get("city") or "").strip()
    city_instruction = f"morando em {city_val}" if city_val else "morando no Brasil (se perguntarem onde mora, responda de forma natural e simpática)"

    use_lead_name = bool(style.get("use_lead_name", False))
    if use_lead_name and lead_name and lead_name.lower() not in ("visitante", "assinante"):
        lead_name_rule = f"SIM — O nome do visitante é '{lead_name}'. Chame-o pelo nome de vez em quando para criar intimidade."
    else:
        lead_name_rule = "NÃO — Não use o nome do perfil do Telegram do visitante; use termos carinhosos naturais (ex: amor, bb, lindo) conforme o nível de flerte."

    length_setting = style.get("length", "Curta")
    if length_setting == "Curta":
        length_rule = "CURTA (1 a 2 frases curtas por mensagem, máximo 220 caracteres, estilo conversa real de WhatsApp/Telegram)."
    elif length_setting == "Longa":
        length_rule = "LONGA (3 a 5 frases detalhadas e envolventes)."
    else:
        length_rule = "MÉDIA (2 a 3 frases equilibradas)."

    emoji_setting = style.get("emojis", "Leve")
    if emoji_setting == "Nenhum":
        emoji_rule = "NENHUM (não use emojis nas respostas)."
    elif emoji_setting == "Frequente":
        emoji_rule = "FREQUENTE (use 2 a 4 emojis expressivos por mensagem)."
    else:
        emoji_rule = "LEVE (use no máximo 1 emoji sutil por mensagem, como 💜, 👀 ou 😈)."

    prompt = f"""Você é uma criadora de conteúdo conversando no seu chat privado do Telegram.
Identidade: {name_instruction} Você tem {persona.get('age', 18)} anos, {city_instruction}.
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
- Espelhar o jeito do visitante: {'Sim (adapte-se ao ritmo e linguagem dele)' if style.get('mirror_visitor', True) else 'Não (mantenha sempre seu próprio estilo)'}
- Usar o nome do lead: {lead_name_rule}
- Tom de voz: {style.get('tone', 'Acolhedor')}
- Tamanho das respostas: {length_rule}
- Uso de emojis: {emoji_rule}

=== OBJETIVO PRINCIPAL ===
{style.get('objective', 'Entender o interesse, lembrar preferências e conduzir naturalmente para o acesso mais adequado.')}

=== REGRAS PRÓPRIAS (OBRIGATÓRIAS) ===
{style.get('rules', '')}
- Palavras de encaminhamento para suporte: {style.get('handoff_words', 'suporte, atendente, problema no pagamento')}

=== MÍDIAS DA CONVERSA ===
Se a mensagem do visitante pedir prévia/foto ou combinar com os sinais de alguma mídia abaixo, inclua exatamente a tag [SEND_MEDIA:ID] no final da sua resposta:
{medias_text if medias_text else 'Nenhuma mídia cadastrada.'}

IMPORTANTE: Converse sempre em primeira pessoa no chat privado do Telegram, sem parecer robô ou suporte técnico. Nunca invente benefícios fora da descrição das ofertas.
"""
    return prompt


async def _call_llm_endpoint(url: str, api_key: str, model: str, messages: List[Dict[str, str]], max_tokens: int = 220) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    json={"model": model, "messages": messages, "temperature": 0.72, "max_tokens": max_tokens}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                    if content:
                        return content
                elif resp.status_code in (401, 402, 403):
                    # Erro de autenticação/saldo: não repete no mesmo endpoint
                    break
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.8)
    return ""


async def generate_ai_reply(
    state: Dict[str, Any],
    history: List[Dict[str, str]],
    user_message: str,
    lead_name: str = ""
) -> Dict[str, Any]:
    """
    Gera a resposta da persona usando:
    1. API configurada pelo usuário (auto-detecta DeepSeek sk-32hex, OpenAI sk-proj, Groq gsk_) com retry automático
    2. Fallback LLM gratuito (OpenAI-compatible via Pollinations) caso a chave esteja sem saldo/inválida
    3. Motor contextual de segurança (nunca repete respostas e responde nome, idade, cidade, FAQ e ofertas)
    """
    system_prompt = build_system_prompt(state, lead_name=lead_name)
    api_key = state.get("account", {}).get("ai_api_key", "").strip()
    provider = state.get("account", {}).get("ai_provider", "deepseek").lower()

    persona = state.get("persona", {})
    business = state.get("business", {})
    style = state.get("style", {})
    offers = [o for o in business.get("offers", []) if o.get("visible", True)]
    faqs = business.get("faqs", [])
    medias = [m for m in state.get("medias", []) if m.get("active", True)]
    bargain = business.get("bargain", {})

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

    # Verifica se o visitante está pedindo desconto / achando caro
    wants_discount = any(
        w in msg_lower for w in [
            "caro", "desconto", "mais barato", "menos", "não tenho", "nao tenho", "só tenho", "so tenho", "abaixa", "promoção", "promocao"
        ]
    )

    # Verifica intenção real de preço / planos / comprar
    strong_offer_words = [
        "preço", "preco", "valor", "quanto custa", "quanto ta", "quanto tá", "quanto é",
        "plano", "assinar", "pix", "comprar"
    ]
    soft_offer_words = ["vip", "entrar", "quero ver", "como funciona", "acesso", "quero"]
    has_strong = any(w in msg_lower for w in strong_offer_words)
    has_soft_combo = sum(1 for w in soft_offer_words if w in msg_lower) >= 2
    wants_offer = (not is_age_question) and (
        wants_discount or has_strong or has_soft_combo
    )

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-14:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if content and not content.startswith("[Downsell]") and not content.startswith("[PIX Gerado"):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    length_setting = style.get("length", "Curta")
    max_tok = 160 if length_setting == "Curta" else (320 if length_setting == "Longa" else 240)

    reply_text = ""

    # 1. Tenta a chave configurada pelo usuário (auto-detectando DeepSeek vs OpenAI vs Groq)
    if api_key:
        endpoints_to_try = []
        if api_key.startswith("gsk_") or provider == "groq":
            endpoints_to_try.append(("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"))
        elif provider == "deepseek" or (api_key.startswith("sk-") and len(api_key) <= 40):
            endpoints_to_try.append(("https://api.deepseek.com/chat/completions", "deepseek-chat"))
            endpoints_to_try.append(("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"))
        else:
            endpoints_to_try.append(("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"))
            endpoints_to_try.append(("https://api.deepseek.com/chat/completions", "deepseek-chat"))

        for url, model in endpoints_to_try:
            try:
                reply_text = await _call_llm_endpoint(url, api_key, model, messages, max_tokens=max_tok)
                if reply_text:
                    break
            except Exception as e:
                print(f"Aviso tentativa LLM ({url}): {e}")

    # 2. Se a chave do usuário estava sem saldo (402) ou houve falha de rede, usa LLM gratuito compatível
    if not reply_text:
        try:
            reply_text = await _call_llm_endpoint("https://text.pollinations.ai/openai", "", "openai", messages, max_tokens=max_tok)
        except Exception as e:
            print(f"Aviso fallback LLM Pollinations: {e}")

    if reply_text:
        for m in medias:
            tag = f"[SEND_MEDIA:{m.get('id')}]"
            if tag in reply_text:
                reply_text = reply_text.replace(tag, "").strip()
                triggered_media = m

        # Extrai eventual tag [DISCOUNT_PRICE:XX.XX] gerada pela IA na negociação
        discount_price: Optional[float] = None
        disc_match = re.search(r"\[DISCOUNT_PRICE:\s*([0-9]+(?:[.,][0-9]+)?)\s*\]", reply_text, flags=re.IGNORECASE)
        if disc_match:
            try:
                discount_price = float(disc_match.group(1).replace(",", "."))
            except Exception:
                pass
            reply_text = re.sub(r"\[DISCOUNT_PRICE:[^\]]+\]", "", reply_text, flags=re.IGNORECASE).strip()

        # Se o usuário pediu desconto, a barganha está ativa e a IA citou um valor em R$ na resposta
        if discount_price is None and wants_discount and bargain.get("enabled") and offers:
            price_mentions = re.findall(r"r\$\s*([0-9]{1,4}(?:[.,][0-9]{1,2})?)", reply_text, flags=re.IGNORECASE)
            if price_mentions:
                try:
                    cand = float(price_mentions[-1].replace(",", "."))
                    eff_min = _compute_effective_min_price(offers, bargain)
                    orig_p = float(offers[0].get("price", 19.0))
                    if eff_min <= cand <= orig_p:
                        discount_price = cand
                except Exception:
                    pass

        # Respeita configuração "Nenhum" emoji caso a IA tenha colocado algum
        if style.get("emojis") == "Nenhum":
            reply_text = re.sub(r"[\U00010000-\U0010ffff]", "", reply_text).strip()

        result: Dict[str, Any] = {
            "reply": reply_text,
            "tokens_used": 180
        }
        if triggered_media:
            result["media"] = triggered_media
        if (wants_offer or discount_price is not None or "r$" in reply_text.lower() or "plano" in reply_text.lower()) and offers:
            chosen_offer = dict(offers[0])
            if discount_price is not None and bargain.get("enabled"):
                eff_min = _compute_effective_min_price(offers, bargain)
                chosen_offer["price"] = max(eff_min, discount_price)
                chosen_offer["name"] = f"{chosen_offer.get('name', 'VIP')} (Desconto)"
                chosen_offer["discounted"] = True
            result["offer_card"] = chosen_offer
        return result

    # 3. Fallback contextual inteligente (caso todas as redes externas oscilem)
    raw_name = (persona.get("name") or "").strip()
    name = raw_name if (raw_name and raw_name.lower() != "atendimento") else "Mel"
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
            "reply": f"pode me chamar de {name} 💜 prazer! e você, como gosta que eu te chame aqui?",
            "tokens_used": 85
        }

    if any(w in msg_lower for w in ["onde você mora", "onde vc mora", "de onde você é", "de onde vc e", "qual cidade", "sua cidade"]):
        return {
            "reply": f"sou de {city} 💜 e você, fala de onde?",
            "tokens_used": 85
        }

    if wants_discount and offers:
        if bargain.get("enabled"):
            eff_min = _compute_effective_min_price(offers, bargain)
            disc_offer = dict(offers[0])
            disc_offer["price"] = eff_min
            disc_offer["name"] = f"{disc_offer.get('name', 'VIP')} (Desconto)"
            disc_offer["discounted"] = True
            return {
                "reply": f"poxa, quero muito te ver lá no meu VIP... consigo fazer uma condição especial de R$ {eff_min:.2f} pra você entrar agora no Pix, topa? 💜",
                "offer_card": disc_offer,
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

    prev_assistant_msgs = {h.get("content", "") for h in history if h.get("role") == "assistant"}
    candidates = [
        "ai sim 💜 gosto de conversar assim no privado rs... você já conhece meu VIP ou é sua primeira vez por aqui?",
        "me conta, o que você mais gosta de ver quando entra num VIP privado? 😈",
        "tô online aqui agora rs... se quiser dar uma olhadinha nos meus planos do VIP é só me falar que te mostro 💜",
        "gostei de você rs 👀 lá no meu VIP eu posto tudo sem censura, quer que eu te explique como funciona o acesso?"
    ]
    for c in candidates:
        if c not in prev_assistant_msgs:
            return {"reply": c, "tokens_used": 110}

    return {"reply": candidates[0], "tokens_used": 110}
