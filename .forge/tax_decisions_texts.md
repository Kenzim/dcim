# Proposed CODEBASE.md change for the tax decisions

Not applied: `CODEBASE.md` belongs to the billing manager. Two changes: one phrase in the existing row "Tax engine (billing phase 2, P2-T)" and one new row.

## 1. In the "Tax engine" row, replace the phrase

`adviser_signoff` (setting `billing_tax_adviser_signoff`, `ADVISER_SIGNOFF_COVERS`)

with

`adviser_signoff` (the operator's acknowledgement that they reviewed the tax defaults; the item key and the setting `billing_tax_adviser_signoff` keep their old names; `ADVISER_SIGNOFF_LABEL`, `ADVISER_SIGNOFF_DETAIL`, `ADVISER_SIGNOFF_COVERS`; the item carries `label` and `detail`, which the billing engines screen and the tax overview show in place of the humanised key)

## 2. New row

| Tax defaults and why (billing phase 2) | `docs/billing-tax-packs.md` section 8 "Defaults and why": each tax default in one line with its reason, checked against `app/services/tax/` and the packs on 2026-11-23, ending with a plain "not legal advice" notice. Design section 18 questions 8 and 10 and 18.1 questions 1 to 9 are marked "Decided by the project chat, 2026-11-23, on the owner's delegation" in `docs/billing-phase2-design.md` (section 5 item 6, 18, 18.1). Q8: VMs, proxies, storage, backup slots and dedicated bare metal are `digital_service`; colocation and rack space need their own class. `digital_service` is created by the first pack apply, not by the migration. |
