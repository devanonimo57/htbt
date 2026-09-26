import httpx
from typing import Optional, Dict, Any

class PushinPayClient:
    """
    Cliente para integração com a API da Pushin Pay.
    Documentação oficial: PIX Automático (Criação de cobrança e Webhook).
    """
    BASE_URL = "https://api.pushinpay.com.br/api"

    def __init__(self, token: str):
        self.token = token
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
        Cria uma cobrança Pix imediata.
        :param value_in_cents: Valor em centavos (ex: R$ 19,90 -> 1990)
        :param webhook_url: URL para receber a notificação do pagamento
        :return: Dict com id da transação, qr_code_base64 e qr_code (copia e cola)
        """
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
        Consulta o status de uma transação específica na Pushin Pay.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self.BASE_URL}/transactions/{transaction_id}",
                headers=self.headers
            )
            response.raise_for_status()
            return response.json()
