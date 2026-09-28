import uuid
import httpx
from typing import Optional, Dict, Any

class PushinPayClient:
    """
    Cliente unificado para integração com Gateways PIX (Pushin Pay, Mercado Pago, OpenPix, Asaas, SuitPay).
    """
    BASE_URL = "https://api.pushinpay.com.br/api"

    def __init__(self, token: str, provider: str = "pushinpay", secret: str = ""):
        self.token = token.strip()
        self.provider = (provider or "pushinpay").lower().strip()
        self.secret = (secret or "").strip()
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    async def create_pix_charge(
        self,
        value_in_cents: int,
        webhook_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Cria uma cobrança Pix imediata no gateway selecionado.
        Retorna dict padronizado com: id, qr_code (copia e cola), qr_code_base64.
        """
        amount_float = round(value_in_cents / 100.0, 2)

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
        payload: Dict[str, Any] = {
            "value": value_in_cents
        }
        if webhook_url:
            payload["webhook_url"] = webhook_url

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.BASE_URL}/pix/cashIn",
                json=payload,
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()

    async def check_transaction_status(self, transaction_id: str) -> Dict[str, Any]:
        """
        Consulta o status de uma transação específica no gateway configurado.
        Retorna {"status": "paid"} quando aprovado.
        """
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

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self.BASE_URL}/transactions/{transaction_id}",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()
