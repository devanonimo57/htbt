import time
import uuid
import httpx
from typing import Optional, Dict, Any

_SYNCPAY_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}


class PushinPayClient:
    """
    Cliente unificado para integração com Gateways PIX (Sync Pay, Pushin Pay, Mercado Pago, OpenPix, Asaas).
    """
    BASE_URL = "https://api.pushinpay.com.br/api"
    SYNCPAY_BASE_URL = "https://api.syncpayments.com.br"

    def __init__(self, token: str, provider: str = "syncpay", secret: str = ""):
        self.token = (token or "").strip()
        self.secret = (secret or "").strip()
        raw_provider = (provider or "syncpay").lower().strip()
        # Se há client_id + client_secret preenchidos, roteia automaticamente para Sync Pay
        if self.secret and raw_provider in ("pushinpay", "suitpay", "syncpay", "sync_pay", ""):
            self.provider = "syncpay"
        else:
            self.provider = raw_provider
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    async def _get_syncpay_bearer(self, force_refresh: bool = False) -> str:
        cache_key = f"{self.token}:{self.secret}"
        now = time.time()
        cached = _SYNCPAY_TOKEN_CACHE.get(cache_key)
        if not force_refresh and cached and cached.get("access_token") and cached.get("expires_at", 0) > now:
            return cached["access_token"]

        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{self.SYNCPAY_BASE_URL}/api/partner/v1/auth-token",
                json={
                    "client_id": self.token,
                    "client_secret": self.secret
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                }
            )
            resp.raise_for_status()
            data = resp.json()
            access_token = data.get("access_token", "")
            expires_in = int(data.get("expires_in", 3600))
            _SYNCPAY_TOKEN_CACHE[cache_key] = {
                "access_token": access_token,
                "expires_at": now + max(300, expires_in - 120)
            }
            return access_token

    async def create_pix_charge(
        self,
        value_in_cents: int,
        webhook_url: Optional[str] = None,
        client_name: str = "Cliente VIP"
    ) -> Dict[str, Any]:
        """
        Cria uma cobrança Pix imediata no gateway selecionado.
        Retorna dict padronizado com: id, qr_code (copia e cola), qr_code_base64.
        """
        amount_float = round(value_in_cents / 100.0, 2)

        if self.provider == "syncpay":
            bearer = await self._get_syncpay_bearer()
            payload = {
                "amount": amount_float,
                "description": "Acesso VIP Privado",
                "webhook_url": webhook_url or "https://hotbot-saas.onrender.com/api/webhook/syncpay",
                "client": {
                    "name": client_name or "Cliente VIP",
                    "cpf": "11144477735",
                    "email": "cliente@vip.app",
                    "phone": "11999999999"
                }
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.SYNCPAY_BASE_URL}/api/partner/v1/cash-in",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Accept": "application/json",
                        "Content-Type": "application/json"
                    }
                )
                if resp.status_code == 401:
                    bearer = await self._get_syncpay_bearer(force_refresh=True)
                    resp = await client.post(
                        f"{self.SYNCPAY_BASE_URL}/api/partner/v1/cash-in",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {bearer}",
                            "Accept": "application/json",
                            "Content-Type": "application/json"
                        }
                    )
                resp.raise_for_status()
                data = resp.json()
                pix_code = data.get("pix_code") or data.get("data", {}).get("pix_code", "")
                identifier = data.get("identifier") or data.get("data", {}).get("reference_id") or str(uuid.uuid4())
                return {
                    "id": str(identifier),
                    "qr_code": pix_code,
                    "qr_code_base64": ""
                }

        if self.provider == "mercadopago":
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": str(uuid.uuid4())
            }
            payload = {
                "transaction_amount": amount_float,
                "description": "Acesso VIP Privado",
                "payment_method_id": "pix",
                "payer": {"email": "assinante@vip.app"}
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post("https://api.mercadopago.com/v1/payments", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                tx_data = data.get("point_of_interaction", {}).get("transaction_data", {})
                return {
                    "id": str(data.get("id")),
                    "qr_code": tx_data.get("qr_code", ""),
                    "qr_code_base64": tx_data.get("qr_code_base64", "")
                }

        if self.provider == "openpix":
            corr_id = str(uuid.uuid4())
            headers = {
                "Authorization": self.token,
                "Content-Type": "application/json"
            }
            payload = {"correlationID": corr_id, "value": value_in_cents, "comment": "Acesso VIP"}
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post("https://api.openpix.com.br/api/v1/charge", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                charge = data.get("charge", {})
                return {
                    "id": charge.get("correlationID", corr_id),
                    "qr_code": charge.get("brCode", data.get("brCode", "")),
                    "qr_code_base64": ""
                }

        if self.provider == "asaas":
            headers = {
                "access_token": self.token,
                "Content-Type": "application/json"
            }
            payload = {"value": amount_float, "description": "Acesso VIP"}
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post("https://api.asaas.com/v3/pix/qrCodes/static", json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return {
                    "id": str(data.get("id", uuid.uuid4())),
                    "qr_code": data.get("payload", ""),
                    "qr_code_base64": data.get("encodedImage", "")
                }

        # Padrão: Pushin Pay
        payload_pp: Dict[str, Any] = {
            "value": value_in_cents
        }
        if webhook_url:
            payload_pp["webhook_url"] = webhook_url

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.BASE_URL}/pix/cashIn",
                json=payload_pp,
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()

    async def check_transaction_status(self, transaction_id: str) -> Dict[str, Any]:
        """
        Consulta o status de uma transação específica no gateway configurado.
        Retorna {"status": "paid"} quando aprovado.
        """
        if self.provider == "syncpay":
            bearer = await self._get_syncpay_bearer()
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    f"{self.SYNCPAY_BASE_URL}/api/partner/v1/transaction/{transaction_id}",
                    headers={
                        "Authorization": f"Bearer {bearer}",
                        "Accept": "application/json"
                    }
                )
                if resp.status_code == 401:
                    bearer = await self._get_syncpay_bearer(force_refresh=True)
                    resp = await client.get(
                        f"{self.SYNCPAY_BASE_URL}/api/partner/v1/transaction/{transaction_id}",
                        headers={
                            "Authorization": f"Bearer {bearer}",
                            "Accept": "application/json"
                        }
                    )
                resp.raise_for_status()
                data = resp.json()
                tx_data = data.get("data", data)
                st = str(tx_data.get("status", "")).lower()
                return {
                    "status": "paid" if st in ("completed", "paid", "approved", "settled") else st,
                    "raw": tx_data
                }

        if self.provider == "mercadopago":
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    f"https://api.mercadopago.com/v1/payments/{transaction_id}",
                    headers={"Authorization": f"Bearer {self.token}"}
                )
                resp.raise_for_status()
                data = resp.json()
                st = str(data.get("status", "")).lower()
                return {"status": "paid" if st == "approved" else st}

        if self.provider == "openpix":
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    f"https://api.openpix.com.br/api/v1/charge/{transaction_id}",
                    headers={"Authorization": self.token}
                )
                resp.raise_for_status()
                data = resp.json()
                st = str(data.get("charge", {}).get("status", "")).upper()
                return {"status": "paid" if st == "COMPLETED" else st.lower()}

        if self.provider == "asaas":
            async with httpx.AsyncClient(timeout=20.0) as client:
                if str(transaction_id).startswith("pay_"):
                    resp = await client.get(
                        f"https://api.asaas.com/v3/payments/{transaction_id}",
                        headers={"access_token": self.token}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    st = str(data.get("status", "")).upper()
                    return {
                        "status": "paid" if st in ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH") else st.lower(),
                        "raw": data
                    }
                resp = await client.get(
                    "https://api.asaas.com/v3/payments",
                    params={"pixQrCodeId": str(transaction_id)},
                    headers={"access_token": self.token}
                )
                resp.raise_for_status()
                data = resp.json()
                payments = data.get("data") if isinstance(data.get("data"), list) else []
                for pmt in payments:
                    if not isinstance(pmt, dict):
                        continue
                    st = str(pmt.get("status", "")).upper()
                    if st in ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"):
                        return {"status": "paid", "raw": pmt}
                first_st = str(payments[0].get("status", "pending")).lower() if payments else "pending"
                return {"status": first_st, "raw": payments[0] if payments else {}}

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self.BASE_URL}/transactions/{transaction_id}",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()

