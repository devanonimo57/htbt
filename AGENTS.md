# Decisions for future repository audits and coding agents

This file records the owner's current scope decisions. A later explicit owner request can supersede them. Do not interpret a known risk as fixed merely because its correction is deferred.

## Do not change without a new request

- **AI Mode scope:** Do not fix the AI-mode flow now unless an issue in it affects the rest of the project. Do not interpret this as a ban on reporting cross-cutting defects.
- **Lock key divergence:** The owner declined an isolated concurrency fix for the old aggregate-state design. This is not proof the issue is resolved. The approved row-based redesign must be tested for concurrent writes, including writes to the same Pix.
- **Order table synchronization:** Do not treat an earlier optimization suggestion as authorization or as a ban on adapting storage code for the approved redesign. Preserve the required payment semantics and test every affected path.
- **Bot peer independence & Remarketing:** All bots are independent peers. Never implement cross-bot lead deduplication in remarketing; each bot may broadcast to its own leads autonomously.
- **Static PIX button:** The inline button `[✅ Verificar pagamento]` is purely static UI feedback. It must never perform outbound HTTP calls to payment gateways or database queries.
- **No gateway polling:** Both gateways prohibit it. Never add automatic, manual, periodic, restart-triggered, or button-triggered payment-status polling. Payment confirmation is webhook-only; duplicate notifications for one Pix must not create another sale.
- **Test-sales cleanup:** The owner excluded the `clear-test-data` atomicity issue from this correction batch. Do not change that flow as collateral work; its pre-commit cache/order risk remains unverified for release.
- **15-minute invite link expiration (Não é falha):** The owner explicitly declared that the 15-minute expiration window for Telegram invite links (including manual access resend) is an intended business requirement. Leads are expected to claim access within 15 minutes or contact support for a fresh link. Do not flag this as a flaw, propose extending it, or modify the expiration window in future audits.
- **Individual lead deletion for funnel tests:** First delete moves only the selected customer to Excluídos; restore reverses it. Permanent deletion there removes that customer's local lead and Pix records, but does not cancel a payable gateway charge. A specific bot filter applies only in that bot; “Todos os bots” applies to that customer across their bots, never all customers. Remove the separate “Limpar todas” bulk action and the Financeiro's per-transaction “Apagar registro” action and their unused backend routes. Verify commit-before-cache/task changes and simultaneous payment writes before release.
- **Diagnostic command token:** The owner explicitly declined fixing the admin token returned by the diagnostic command. Do not change that route or its command display under an unrelated task; this is risk acceptance, not proof that the token cannot be exposed.
- **Credentials in saved profiles:** A later explicit request approved fixing secret exposure in saved profile snapshots, both in authenticated API responses and browser storage. Preserve the ability to restore a profile's configured credentials without exposing them in snapshots; do not treat this as permission to change diagnostic commands.
- **TLS/deployment topology:** The owner will inspect the live VPS later. Do not assume the deployment currently uses HTTP; do not replace their existing VPS update command or deploy/configure a proxy without a request.
- **Update/lead task queue:** Do not change its admission, concurrency, or backpressure behavior under an unrelated task.
- **Multiple application processes:** The owner reports running one panel/instance in one PowerShell. Do not prioritize cross-process delivery idempotency unless the deployment changes or they ask.
- **Delivery-pending dashboard:** Do not add or conflate a delivery-pending KPI with *unpaid Pix* metrics. Internal paid-but-undelivered state remains necessary for the bounded automatic retry; the owner prefers customer support through the bot's Telegram contact rather than a new operator queue. Discuss any removal of existing access controls separately.

## Product requirements to preserve

- Every distinct paid Pix ID is a distinct legitimate sale, even for the same customer and offer. A new checkout must not make an older payable Pix invisible if it later settles.
- **Leave repeat-plan purchases as-is:** when a customer pays for a higher or lower plan later, send the destination attached to that new paid Pix and do not revoke access already acquired from an earlier purchase. Do **not** compare plans, calculate missing benefits, implement differential delivery, or infer overlaps from plan names/prices. The owner explicitly chose the existing behavior over an earlier illustrative "only the missing benefit" idea.
- Automatic delivery budget is the initial send plus **one** retry per Pix; afterward keep the payment confirmed and stop automatic attempts. Never deliver a purchase using a different Telegram bot merely because the origin is unavailable.
- In the CRM, distinguish leads and transactions using Telegram user ID, configured bot ID, and the immutable public numeric origin when known. Administrative messages, blocking, and remarketing must not pick another bot for a lead with uncertain origin; old unverifiable leads can require manual inspection instead of automatic send. Reusing a configured bot ID for another Telegram bot must not reuse the former bot's lead history.

## Approved work, not a claim of implementation

- Replace the aggregate JSON persistence of clients and Pix with independent database rows per customer+immutable bot origin and per Pix ID; keep configuration separate. Do not create one physical JSON file per customer or payment. This SaaS has not yet been used with real leads or sales, so do not describe migration of production history as a current obstacle. Test concurrent writes and all checkout, webhook, delivery, CRM, and reporting paths before calling the redesign complete.
- In Painel, calculate display metrics from saved records when opening the view or clicking “Atualizar métricas” immediately left of “HOJE”, not by rewriting aggregate counters after every action. Webhooks still persist payment and delivery immediately. The refresh never contacts a payment gateway. Remove fabricated “Visitas”, its visits→starts percentage, and dependent estimates; keep measured starts and sales.
- Financeiro totals must follow the selected period and bot; a confirmed Pix contributes to the period of its payment. Avoid claiming open Pix face values are expected revenue. The proposed on/off pending-metric selector has **not** been approved.

## Unresolved questions, not decisions

Questions or acknowledgments about test payments, gateway fees, access-removal confirmation, AI resend, injected HTML, or update scripts are not blanket authorization. Earlier claims that database/cache concurrency was fully fixed and verified were contradicted by later reproductions; do not copy them into completion reports. Only a passing integrated suite and inspected diff can establish the present implementation's status.

