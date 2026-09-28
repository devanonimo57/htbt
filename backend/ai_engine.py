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


def _extract_introduced_name(history: List[Dict[str, str]], current_msg: str) -> str:
    patterns = [
        r"(?:meu nome [eé]|me chamo|pode me chamar de|sou o|sou a)\s+([A-ZÀ-Úa-zà-ú]{2,20})",
    ]
    all_user_texts = [h.get("content", "") for h in history if h.get("role") == "user"] + [current_msg]
    for txt in reversed(all_user_texts):
        for pat in patterns:
            m = re.search(pat, txt, flags=re.IGNORECASE)
            if m:
                cand = m.group(1).strip()
                if cand.lower() not in ("um", "uma", "aqui", "novo", "nova", "eu", "voce", "você", "cliente"):
                    return cand.capitalize()
    return ""


def _analyze_visitor_mirroring(user_message: str) -> str:
    words = [w for w in re.split(r"\s+", user_message.strip()) if w]
    w_count = len(words)
    msg_low = user_message.lower()

    if w_count <= 4:
        size_hint = f"curtíssima ({w_count} palavra(s)) -> responda curto na mesma proporção (1 frase rápida)"
    elif w_count <= 12:
        size_hint = f"média/direta ({w_count} palavras) -> acompanhe com 1 a 2 frases"
    else:
        size_hint = f"longa/detalhada ({w_count} palavras) -> acompanhe desenvolvendo mais a resposta"

    slang_tokens = ("vc", "tb", "tbm", "pq", "qnd", "cmg", "ctz", "blz", "opa", "eae", "kk", "rs", "mano", "gata", "linda", "bb")
    formal_tokens = ("olá", "bom dia", "boa tarde", "boa noite", "gostaria", "poderia", "por gentileza", "como funciona")
    if any(tok in msg_low for tok in slang_tokens):
        informality_hint = "informal/descontraído -> espelhe linguagem solta de chat"
    elif any(tok in msg_low for tok in formal_tokens):
        informality_hint = "educado/direto -> mantenha simpatia sem exagerar nas gírias"
    else:
        informality_hint = "neutro/natural -> mantenha conversa fluida"

    if "!" in user_message or any(e in user_message for e in ("😍", "😈", "🔥", "👀", "❤️", "💜")):
        energy_hint = "energia alta/entusiasmada -> devolva na mesma intensidade calorosa"
    elif w_count <= 3 and "?" not in user_message:
        energy_hint = "energia baixa/econômica -> seja leve e faça uma pergunta simples para destravar"
    else:
        energy_hint = "energia engajada/curiosa -> responda o ponto principal e conduza o próximo passo"

    return f"Leitura da última mensagem do visitante: Tamanho {size_hint} | Informalidade: {informality_hint} | Energia: {energy_hint}."


def build_system_prompt(
    state: Dict[str, Any],
    lead_name: str = "",
    visitor_mirror_signal: str = "",
    already_discounted: bool = False
) -> str:
    """
    Constrói o System Prompt completo da Persona com todos os parâmetros do Sharkbot:
    - Persona e identidade (nome, idade, apresentação/gênero, cidade, biografia curta, traços)
    - Negócio e ofertas (contexto da criadora, ofertas visíveis, negociação de preço, dúvidas frequentes)
    - Jeito de conversar (estratégia comercial, nível de flerte, gírias, cadência de conversão, espelhamento, tom, tamanho, emojis, objetivo, regras próprias)
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

    active_medias = [
        m for m in medias
        if m.get("active", True) and "Conversa da IA" in (m.get("used_in") or ["Conversa da IA"])
    ]
    medias_text = "\n".join([
        f"- [SEND_MEDIA:{m.get('id')}] Nome: {m.get('name')} | Tipo: {m.get('type')} | O que mostra: {m.get('description')} | Objetivo: {m.get('objective')} | Sinais: {m.get('signals')}"
        for m in active_medias
    ])

    bargain = business.get("bargain", {})
    eff_min = _compute_effective_min_price(visible_offers, bargain)
    if bargain.get("enabled"):
        if bargain.get("once_per_visitor") and already_discounted:
            bargain_rule = (
                f"JÁ UTILIZADA NESTA CONVERSA (Você já concedeu a condição especial de R$ {eff_min:.2f} para este visitante. "
                f"Não baixe mais o valor; reforce com carinho que essa condição já é o menor preço possível)."
            )
        else:
            bargain_rule = (
                f"ATIVADA (Desconto máximo: {bargain.get('max_discount_pct', 40)}% | "
                f"Preço mínimo autorizado: R$ {eff_min:.2f} | Validade da proposta: {bargain.get('validity_minutes', 30)} min). "
                f"Se o visitante achar caro, pedir desconto ou disser que tem pouco dinheiro, ofereça um valor promocional entre R$ {eff_min:.2f} e o valor original, "
                f"e inclua no final da resposta a tag [DISCOUNT_PRICE:VALOR] (ex: [DISCOUNT_PRICE:{eff_min:.2f}]) para atualizar o botão do PIX automaticamente."
            )
    else:
        bargain_rule = "DESATIVADA (Preços estritamente fixos, nunca dê descontos nem baixe o valor)."

    gender_val = (persona.get("gender") or "Feminina").strip()
    gender_desc = (persona.get("gender_desc") or "Fala e se apresenta no feminino.").strip()
    raw_name = (persona.get("name") or "").strip()
    if not raw_name or raw_name.lower() == "atendimento":
        if gender_val == "Masculina":
            name_instruction = "Apresente-se como o criador dono do VIP (nunca diga 'meu nome é Atendimento'; se perguntarem seu nome, use um apelido masculino natural como Léo ou Gui)."
        elif gender_val == "Neutra":
            name_instruction = "Apresente-se como responsável pelo VIP (nunca diga 'meu nome é Atendimento'; evite marcações de gênero ao falar de si)."
        else:
            name_instruction = "Apresente-se como a criadora dona do VIP (nunca diga 'meu nome é Atendimento'; se perguntarem seu nome, use um apelido feminino natural como Mari ou Mel)."
    else:
        name_instruction = f"Seu nome é {raw_name}. Sempre use esse nome quando perguntarem."

    city_val = (persona.get("city") or "").strip()
    city_instruction = f"morando em {city_val}" if city_val else "morando no Brasil (se perguntarem onde mora, responda de forma natural e simpática)"

    # Estratégia comercial (define em que momento a oferta entra na conversa)
    strategy_setting = style.get("strategy", "Conexão primeiro")
    if strategy_setting == "Direto":
        strategy_rule = (
            "DIRETO — Apresente a oferta cedo na conversa, já explicando o acesso ao VIP logo nas primeiras respostas, "
            "sem perder a naturalidade."
        )
    elif strategy_setting == "Equilibrado":
        strategy_rule = (
            "EQUILIBRADO — Crie uma troca curta (1 a 2 mensagens reagindo genuinamente ao que o visitante disse) "
            "e aproveite o primeiro gancho de curiosidade/interesse para apresentar o VIP."
        )
    else:
        strategy_rule = (
            "CONEXÃO PRIMEIRO — Converse de verdade com o visitante e entenda o interesse dele ANTES de falar de VIP ou vender. "
            "Se o visitante responder apenas 'sim', 'não', cumprimentar ou puxar qualquer assunto casual/aleatório (ex: passear, "
            "andar de carro, rotina, gostos), entre no assunto dele com naturalidade usando sua personalidade e NÃO fale de VIP "
            "nem ofereça planos até que a conversa crie conexão ou que ele demonstre interesse real em ver mais sobre você/seu conteúdo."
        )

    # Nível de flerte
    flirt_setting = style.get("flirt", "Leve")
    if flirt_setting == "Nenhum":
        flirt_rule = "NENHUM — Seja simpática, educada e receptiva, mas sem paquera nem apelidos íntimos."
    elif flirt_setting == "Sensual":
        flirt_rule = "SENSUAL — Provocante, sem descrição gráfica (instigue desejo e curiosidade pelo que há no VIP com charme e malícia sutil, sem textos sexuais explícitos)."
    else:
        flirt_rule = "LEVE — Carinhosa e charmosa na medida certa, usando apelidos suaves (ex: bb, amor, lindo) com naturalidade."

    # Uso de gírias
    slang_setting = style.get("slang", "Natural")
    if slang_setting == "Nenhuma":
        slang_rule = "NENHUMA — Escreva sem gírias e sem abreviações de internet."
    elif slang_setting == "Marcante":
        slang_rule = "MARCANTE — Use bastante informalidade, abreviações (vc, tô, tá, pra, rs) e expressões naturais de chat brasileiro."
    else:
        slang_rule = "NATURAL — Use linguagem fluida de chat privado (vc, tô, rs) de forma moderada e espontânea."

    # Cadência (controla a sensação de velocidade e pressão comercial na conversa)
    cadence_setting = style.get("cadence", "Natural")
    if cadence_setting == "Ágil":
        cadence_rule = "ÁGIL — Balões curtos e avanço rápido (respostas diretas, ritmo acelerado, conduzindo o visitante rapidamente para o próximo passo)."
    elif cadence_setting in ("Calmo", "Calma"):
        cadence_rule = "CALMO — Mais acolhimento e menos pressão (priorize deixar o visitante confortável, ouça e valide o que ele diz sem apressar o fechamento)."
    else:
        cadence_rule = "NATURAL — Alterna reação, pergunta e próximo passo (reaja genuinamente ao que o visitante disse, faça uma pergunta envolvente e avance com naturalidade)."

    # Espelhar o jeito do visitante
    mirror_enabled = style.get("mirror_visitor", True)
    if mirror_enabled:
        mirror_rule = (
            "SIM — Acompanhe a energia, o tamanho das mensagens e o nível de informalidade do visitante. "
            + (f"({visitor_mirror_signal})" if visitor_mirror_signal else "")
        )
    else:
        mirror_rule = "NÃO — Mantenha sempre seu próprio estilo, ritmo e tamanho de resposta configurados abaixo, independente de como o visitante escreva."

    # Usar o nome do lead
    use_lead_name = bool(style.get("use_lead_name", False))
    if use_lead_name and lead_name and lead_name.lower() not in ("visitante", "assinante"):
        lead_name_rule = f"SIM — Memorize e use o nome '{lead_name}' com naturalidade depois que a pessoa interagir/se apresentar, mas SEM repetir em toda mensagem."
    else:
        lead_name_rule = "NÃO — Não chame o visitante pelo nome do perfil; use apenas pronomes ou termos naturais compatíveis com o nível de flerte."

    # Tom de voz
    tone_setting = style.get("tone", "Acolhedor")
    if tone_setting == "Direto":
        tone_rule = "DIRETO (Objetiva, segura e sem rodeios)."
    elif tone_setting == "Descontraído":
        tone_rule = "DESCONTRAÍDO (Leve, divertida, bem-humorada e espontânea)."
    elif tone_setting in ("Premium", "Provocante"):
        tone_rule = "PREMIUM (Exclusiva, magnética, confiante e de alto valor percebido)."
    else:
        tone_rule = "ACOLHEDOR (Calorosa, próxima e atenciosa, fazendo o visitante se sentir especial)."

    # Tamanho das respostas ("Curta" | "Média" | "Detalhada" / "Longa")
    length_setting = style.get("length", "Curta")
    if length_setting == "Curta":
        length_rule = "CURTA (1 a 2 frases curtas por mensagem, máximo 220 caracteres, estilo conversa real de Telegram)."
    elif length_setting in ("Detalhada", "Longa"):
        length_rule = "DETALHADA (3 a 5 frases completas, explicando bem os detalhes e criando envolvimento)."
    else:
        length_rule = "MÉDIA (2 a 3 frases equilibradas)."

    # Uso de emojis ("Nenhum" | "Leve" | "Moderado" / "Frequente")
    emoji_setting = style.get("emojis", "Leve")
    if emoji_setting == "Nenhum":
        emoji_rule = "NENHUM (não use emojis nas respostas)."
    elif emoji_setting in ("Moderado", "Frequente"):
        emoji_rule = "MODERADO (use 2 a 3 emojis expressivos que combinem com o contexto da frase)."
    else:
        emoji_rule = "LEVE (use no máximo 1 emoji sutil por mensagem, como 💜, 👀 ou 😈)."

    support_user = state.get("publish", {}).get("support_username", "").strip()
    support_info = f" (Caso precise encaminhar para suporte, indique @{support_user.lstrip('@')})" if support_user else ""

    prompt = f"""Você é a pessoa definida na Identidade abaixo, conversando em primeira pessoa no seu chat privado do Telegram.
Leia todo o contexto do que o visitante está dizendo e responda como uma pessoa real, trabalhando 100% em conjunto com sua Persona, sua Estratégia Comercial e suas Regras.

=== SUA IDENTIDADE E PERSONA ===
- Quem você é: {name_instruction} Você tem {persona.get('age', 18)} anos, {city_instruction}.
- Apresentação / Gênero: {gender_val} ({gender_desc} Flexione todas as palavras sobre si de acordo com esta apresentação).
- Biografia curta: {persona.get('bio', '')}
- Traços de personalidade: {persona.get('traits', 'espontânea, atenta, bem-humorada')}

=== CONTEXTO DA CRIADORA E DO VIP ===
{business.get('context', '')}

=== OFERTAS DISPONÍVEIS ===
{offers_text if offers_text else 'Nenhuma oferta ativa.'}

=== NEGOCIAÇÃO DE PREÇO ===
{bargain_rule}

=== DÚVIDAS QUE VOCÊ JÁ SABE RESPONDER (FAQ) ===
{faqs_text if faqs_text else 'Responda com base no contexto.'}

=== JEITO DE CONVERSAR E HUMANIZAÇÃO DA VENDA ===
- Estratégia comercial: {strategy_rule}
- Cadência de conversão: {cadence_rule}
- Nível de flerte: {flirt_rule}
- Uso de gírias: {slang_rule}
- Espelhar o jeito do visitante: {mirror_rule}
- Usar o nome do lead: {lead_name_rule}
- Tom de voz: {tone_rule}
- Tamanho das respostas: {length_rule}
- Uso de emojis: {emoji_rule}

=== OBJETIVO PRINCIPAL ===
{style.get('objective', 'Entender o interesse, lembrar preferências e conduzir naturalmente para o acesso mais adequado.')}

=== REGRAS PRÓPRIAS (OBRIGATÓRIAS) ===
{style.get('rules', '')}
- Palavras de encaminhamento para suporte: {style.get('handoff_words', 'suporte, atendente, problema no pagamento')}{support_info}

=== COMANDOS INTERNOS DE AÇÃO PELO CONTEXTO (VOCÊ DECIDE QUANDO ACIONAR) ===
1. Exibir botão dos planos ([SHOW_OFFERS]):
   - Inclua a tag [SHOW_OFFERS] no final da sua resposta SOMENTE quando você estiver apresentando ou oferecendo o acesso ao seu VIP/planos na conversa (respeitando sua Estratégia Comercial), ou quando você entender pelo contexto que o visitante realmente perguntou sobre seu conteúdo pago, preços, planos ou como entrar no VIP.
   - NUNCA inclua [SHOW_OFFERS] quando o visitante estiver apenas dizendo 'sim', 'não', cumprimentando ou falando de assuntos aleatórios/casuais (ex: 'quero andar de carro', rotina, bate-papo inicial).
2. Aplicar desconto negociado ([DISCOUNT_PRICE:VALOR]):
   - Se a Negociação de Preço estiver ATIVADA acima e você entender pelo contexto real da conversa que o visitante achou o valor do VIP caro ou pediu desconto para entrar, proponha o valor promocional dentro do limite autorizado e inclua [DISCOUNT_PRICE:VALOR] (ex: [DISCOUNT_PRICE:{eff_min:.2f}]) no final da resposta.
3. Enviar mídia da conversa ([SEND_MEDIA:ID]):
   - Se pelo contexto real da conversa o visitante pedir uma prévia/foto/amostra ou o momento combinar com a descrição e o objetivo de uma das mídias ativas abaixo, inclua [SEND_MEDIA:ID] no final da sua resposta:
{medias_text if medias_text else 'Nenhuma mídia cadastrada.'}

IMPORTANTE: Converse sempre em primeira pessoa no chat privado do Telegram, reagindo de verdade ao assunto que o visitante trouxe, sem parecer robô ou vendedor automático. Nunca invente benefícios fora da descrição das ofertas.
"""
    return prompt


async def _call_llm_endpoint(url: str, api_key: str, model: str, messages: List[Dict[str, str]], max_tokens: int = 220) -> str:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
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
    Gera a resposta da persona usando leitura 100% contextual via LLM (DeepSeek / OpenAI / Groq)
    trabalhando em conjunto com toda a configuração da Persona, Estratégia Comercial, Negócio e Mídias.
    """
    persona = state.get("persona", {})
    business = state.get("business", {})
    style = state.get("style", {})
    offers = [o for o in business.get("offers", []) if o.get("visible", True)]
    faqs = business.get("faqs", [])
    medias = [
        m for m in state.get("medias", [])
        if m.get("active", True) and "Conversa da IA" in (m.get("used_in") or ["Conversa da IA"])
    ]
    bargain = business.get("bargain", {})

    # Verifica se o visitante se apresentou no chat ("meu nome é X", "me chamo X") ou usa lead_name
    introduced_name = _extract_introduced_name(history, user_message)
    effective_lead_name = introduced_name or lead_name

    # Verifica se já houve desconto concedido nesta conversa (para respeitar once_per_visitor)
    already_discounted = any(
        "(Desconto)" in h.get("content", "") or "condição especial" in h.get("content", "").lower()
        for h in history if h.get("role") == "assistant"
    )

    visitor_mirror_signal = _analyze_visitor_mirroring(user_message) if style.get("mirror_visitor", True) else ""
    system_prompt = build_system_prompt(
        state,
        lead_name=effective_lead_name,
        visitor_mirror_signal=visitor_mirror_signal,
        already_discounted=already_discounted
    )

    api_key = state.get("account", {}).get("ai_api_key", "").strip()
    provider = state.get("account", {}).get("ai_provider", "deepseek").lower()

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-14:]:
        role = h.get("role", "user")
        content = h.get("content", "")
        if content and not content.startswith("[Downsell]") and not content.startswith("[PIX Gerado"):
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    length_setting = style.get("length", "Curta")
    max_tok = 160 if length_setting == "Curta" else (320 if length_setting in ("Detalhada", "Longa") else 240)
    # Se espelhamento está ativo e a mensagem do visitante for curtíssima, evita resposta longa desproporcional
    if style.get("mirror_visitor", True) and len(user_message.split()) <= 4:
        max_tok = min(max_tok, 140)

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
        triggered_media = None
        for m in medias:
            tag = f"[SEND_MEDIA:{m.get('id')}]"
            if tag in reply_text:
                reply_text = reply_text.replace(tag, "").strip()
                triggered_media = m
        reply_text = re.sub(r"\[SEND_MEDIA:[^\]]+\]", "", reply_text, flags=re.IGNORECASE).strip()

        show_offers_tag = False
        if re.search(r"\[SHOW_OFFERS\]", reply_text, flags=re.IGNORECASE):
            show_offers_tag = True
            reply_text = re.sub(r"\[SHOW_OFFERS\]", "", reply_text, flags=re.IGNORECASE).strip()

        # Extrai eventual tag [DISCOUNT_PRICE:XX.XX] gerada pela IA na negociação
        discount_price: Optional[float] = None
        disc_match = re.search(r"\[DISCOUNT_PRICE:\s*([0-9]+(?:[.,][0-9]+)?)\s*\]", reply_text, flags=re.IGNORECASE)
        if disc_match:
            try:
                discount_price = float(disc_match.group(1).replace(",", "."))
            except Exception:
                pass
            reply_text = re.sub(r"\[DISCOUNT_PRICE:[^\]]+\]", "", reply_text, flags=re.IGNORECASE).strip()

        if bargain.get("once_per_visitor") and already_discounted:
            discount_price = None

        # Respeita configuração "Nenhum" emoji caso a IA tenha colocado algum
        if style.get("emojis") == "Nenhum":
            reply_text = re.sub(r"[\U00010000-\U0010ffff]", "", reply_text).strip()

        result: Dict[str, Any] = {
            "reply": reply_text,
            "tokens_used": 180
        }
        if triggered_media:
            result["media"] = triggered_media
        if (show_offers_tag or discount_price is not None) and offers:
            chosen_offer = dict(offers[0])
            if discount_price is not None and bargain.get("enabled"):
                eff_min = _compute_effective_min_price(offers, bargain)
                chosen_offer["price"] = max(eff_min, discount_price)
                chosen_offer["name"] = f"{chosen_offer.get('name', 'VIP')} (Desconto)"
                chosen_offer["discounted"] = True
            result["offer_card"] = chosen_offer
        return result

    # 3. Fallback contextual inteligente (apenas caso todas as redes externas estejam offline)
    msg_lower = user_message.lower().strip()
    is_age_question = any(w in msg_lower for w in ["quantos anos", "sua idade", "que idade", "idade vc tem", "idade você tem"])
    wants_discount = any(w in msg_lower for w in ["caro", "desconto", "mais barato"])
    wants_offer = any(w in msg_lower for w in ["preço", "preco", "valor", "quanto custa", "planos", "assinar", "pix"])
    triggered_media = None
    raw_name = (persona.get("name") or "").strip()
    gender_val = (persona.get("gender") or "Feminina").strip()
    default_nick = "Léo" if gender_val == "Masculina" else ("Alex" if gender_val == "Neutra" else "Mel")
    name = raw_name if (raw_name and raw_name.lower() != "atendimento") else default_nick
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
