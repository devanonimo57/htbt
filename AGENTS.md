# Decisions for future repository audits and coding agents

This file records the owner's current scope decisions. A later explicit owner request can supersede them. Do not interpret a known risk as fixed merely because its correction is deferred.

## Do not change without a new request

- **Lead deletion for funnel tests:** The owner uses deletion to repeat funnel tests. Do not redesign deletion or automatically restore/confirm deleted leads merely because an old Pix can still be paid. The gateway charge may remain payable after local deletion; mention that trade-off when relevant.
- **Credentials in saved profiles:** The owner declined this remediation for now. Keep reporting a verified exposure when auditing security, but do not silently alter profile storage or redaction as part of unrelated work. This is risk acceptance, not proof that access to the computer or admin session is impossible.
- **TLS/deployment topology:** The owner will inspect the live VPS later. Do not assume the deployment currently uses HTTP; do not replace their existing VPS update command or deploy/configure a proxy without a request.
- **Update/lead task queue:** Do not change its admission, concurrency, or backpressure behavior under an unrelated task.
- **Multiple application processes:** The owner reports running one panel/instance in one PowerShell. Do not prioritize cross-process delivery idempotency unless the deployment changes or they ask.
- **Delivery-pending dashboard:** Do not add or conflate a delivery-pending KPI with *unpaid Pix* metrics. Internal paid-but-undelivered state remains necessary for the bounded automatic retry; the owner prefers customer support through the bot's Telegram contact rather than a new operator queue. Discuss any removal of existing access controls separately.

## Product requirements to preserve

- Distinct paid Pix IDs are distinct legitimate sales, even for the same customer and offer. A new checkout must not make an older payable Pix invisible if it later settles.
- Purchased access is **additive, not downgraded**: if plan B includes everything in A plus another benefit, paying A then B should add only the missing benefit; paying B then A must not remove the extra benefit. The current plan/delivery model may not encode individual benefits. Clarify how real plans map to benefits/channels before implementing a differential grant; never infer it from plan names or prices.
- Automatic delivery budget is the initial send plus **one** retry per Pix; afterward keep the payment confirmed and stop automatic attempts. Never deliver a purchase using a different Telegram bot merely because the origin is unavailable.
- In the CRM, distinguish leads and transactions using Telegram user ID, configured bot ID, and the immutable public numeric origin when known. Administrative messages, blocking, and remarketing must not pick another bot for a lead with uncertain origin; old unverifiable leads can require manual inspection instead of automatic send. Reusing a configured bot ID for another Telegram bot must not reuse the former bot's lead history.

## Unresolved questions, not decisions

The owner asked for explanations of test-payment behavior, gateway gross/net/fees, access-removal confirmation, AI resend, database failures, old-sale counters, injected HTML, and update scripts. Do not treat a question or an acknowledgment as blanket authorization to change those paths.
