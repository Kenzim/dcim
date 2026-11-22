# Tax rule packs: operator guide

Rackflow ships two VAT rule packs, `uk_vat` (UK VAT) and `eu_vat` (EU VAT). This guide explains what they contain, how to apply and update them, how VAT numbers are checked, and what you must decide yourself.

**The packs are starting points, not tax advice.** They encode one reading of the UK and EU rules for a business selling hosting and similar services online (`docs/billing-phase2-design.md`, appendix C). Whether they fit your business, which registrations you need, which tax class each product has and what your invoices must say are questions for your tax adviser. Rackflow produces the documents; compliance is the operator's responsibility.

## 1. What a pack is

A pack is a versioned JSON file (`app/services/tax/packs/uk_vat.json` and `eu_vat.json`) listing tax rules, legal note texts and metadata. Applying a pack copies its rules into the `tax_rules` table; after that the rules are ordinary rows that you can see, disable or supersede in the tax admin screens. Nothing happens at deploy: an installation has no pack rules until an administrator applies a pack, and Rackflow never applies or updates a pack on its own.

Third-party packs can also come from plugins of kind `tax_rule_pack`. They are listed as `<plugin_id>:<provider_id>` next to the built-in packs, and they are previewed and applied in exactly the same way. A plugin pack that fails to load is shown as unavailable; rules applied from it before are not affected.

### 1.1 What each rule says

Every rule has a country (or none, meaning any country), a tax class, a customer type (`any`, `business` or `consumer`), a rate, and the **jurisdiction** it needs: the seller registration under which the rule may be used.

- `GB`: you hold a UK VAT registration.
- An EU country code such as `DE`: you hold a domestic VAT registration in that member state.
- `EU_OSS`: you are registered for the One Stop Shop (union scheme if you are established in the EU, non-union scheme if you are established outside it).

A rule only applies when the tax engine chose the matching registration for the sale. The registrations are yours to enter (tax admin, registrations); a pack never creates one.

### 1.2 The UK pack (`uk_vat`, version 2026.11.1)

- UK VAT 20% for the `standard` and `digital_service` classes, 5% for `reduced` and 0% for `zero`, all under `GB`.
- Zero-tax reverse charge rows for business customers outside the UK with a valid tax ID: to an EU business the note `gb_outside_scope_b2b` ("Outside the scope of UK VAT: reverse charge. The customer accounts for any VAT due."), to any other business the note `outside_scope`.
- No UK domestic reverse charge: it covers construction, some goods, wholesale telecoms and energy, not hosting.
- Northern Ireland is treated as the UK for services (country `GB`); `XI` numbers are read as GB numbers.

### 1.3 The EU pack (`eu_vat`, version 2026.11.1)

For each of the 27 member states and each of the `standard` and `digital_service` classes, three rows at that state's standard rate:

- `eu_<CC>_<class>_domestic`: country `<CC>`, jurisdiction `<CC>`. Used when you hold a domestic registration in that state and the customer is there.
- `eu_<CC>_<class>_oss`: country `<CC>`, jurisdiction `EU_OSS`. Used for consumers in that state when you charge their VAT through the One Stop Shop.
- `eu_<CC>_<class>_seller_state`: **no country**, jurisdiction `<CC>`, priority -10. See 1.4.

And, for each member state as the seller's jurisdiction, zero-tax reverse charge rows for business customers with a valid VAT number: in another member state with the note `eu_reverse_charge` ("Reverse charge: VAT to be accounted for by the customer (Article 196, Directive 2006/112/EC)."), outside the EU with the note `outside_scope`.

Greece is stored under its ISO code `GR` in rules; its VAT numbers use the prefix `EL`, which is what is sent to VIES.

Only standard rates are included, because the packs treat everything Rackflow sells as standard rated electronically supplied services. Regional rates (the Azores, Madeira) and territories outside the EU VAT area (for example the Canary Islands, Ceuta, Melilla, the Aland Islands, Mount Athos and the French overseas departments) are not covered: the customer's billing country decides.

### 1.4 What the seller-state rows are for

A seller established in an EU member state that sells electronically supplied services to consumers in other member states and stays under the EU-wide 10,000 euro threshold may charge its own state's VAT instead of the customer's state's VAT, without registering for the One Stop Shop. In that case the tax engine issues the invoice under the seller's own registration (jurisdiction, say, `DE`) for a customer in another state (say, France).

The ordinary domestic row `eu_DE_standard_domestic` has country `DE`, so it would not match a French customer. The row `eu_DE_standard_seller_state` has no country, so it matches any customer under the `DE` registration and charges the German rate. Its priority of -10 keeps it below every other row under that registration: a German customer still matches the domestic row (a rule with a country is more specific than one without), and a business with a valid VAT number in another state still gets the reverse charge row (priority 10 or 0).

Rackflow does not track the 10,000 euro threshold. You declare which case you are in by your registrations: hold an `EU_OSS` registration and the customer's state's rate applies; do not hold one and your own state's rate applies through these rows. If you pass the threshold, register for the One Stop Shop and enter the registration before your next invoices. If you never need the under-threshold treatment, you can disable the `_seller_state` rows after applying the pack.

**The threshold rules change with ViDA from 1 January 2027; check them before relying on these rows.** Council Directive (EU) 2025/516 ("VAT in the Digital Age"), Article 2, point (4), amends Article 59c of the VAT Directive with effect from 1 January 2027; member states had to transpose it by 31 December 2026 (Article 6(2)). Recital 36 says the aim is that only distance sales of goods dispatched from the seller's own member state count towards the 10,000 euro, not sales from stock held in another member state. As we read the amended text, the condition for services (supplied to a non-taxable person in another member state) is reworded but not changed in substance, so nothing changes for electronically supplied services. Two points remain for you and your adviser: the threshold still adds electronically supplied services and distance sales of goods together, and the option to tax at destination anyway still binds you for at least two calendar years (Article 59c(3), which the directive restates). Check how your member state transposed Article 2 before 1 January 2027.

### 1.5 Rounding

Both packs round per invoice per rate, half up: the bases of all lines sharing a rule are added, the VAT on the total is rounded to the nearest minor unit (half a penny or cent goes up), and the result is spread back over the lines so they add up exactly. HMRC also accepts rounding the invoice's VAT down, and some member states have their own rules; confirm with your adviser (owner question 1).

## 2. Applying a pack

1. Enter your registrations and set `billing_tax_seller_country` to your country of establishment.
2. **Check the rates yourself before you apply.** Each pack's `metadata` has the date the rates were checked (`rates_checked_on`), the sources used and an `unconfirmed` list. In particular, rate changes scheduled after 22 November 2026 were **not** checked against the European Commission's Taxes in Europe Database (TEDB, <https://ec.europa.eu/taxation_customs/tedb/#/vat-search>). Look there, and at HMRC's guidance, for any change announced since that date. Rates change often, and most changes touch reduced rates on particular goods. Examples reported in secondary sources (not checked here) are Latvia's 12% rate on certain foods ending on 30 June 2027, Sweden's 6% rate on food ending on 31 December 2027, and UK domestic electricity returning from 0% to 5% on 1 April 2027. Changes like these do not change the rates in these packs: the EU pack carries standard rates only, and the UK pack's `reduced` (5%) and `zero` (0%) classes are rates, not lists of goods. They would only change which class a product belongs to, which matters if you sell such goods. A change to a **standard** rate, of any member state or the UK, needs a new pack version or an admin rule from its effective date.
3. In the Packs tab, choose the pack and an **effective from** date and time. It may not be in the past: applying a pack never changes the rate of an invoice already issued. Choosing a date a few days ahead gives you time to review the result.
4. Read the preview. Every rule key is listed as `add`, `unchanged`, `change` (with the old and new values) or `remove`. Legal notes are listed as `add`, `unchanged`, `change`, or `kept_edited` when an administrator edited the text (an edited text is always kept). A tax class the pack needs and you do not have, such as `digital_service`, is listed as an addition.
5. Apply. The apply uses exactly the preview you saw: if the pack or the applied rules changed since, it is refused (`pack_changed`) and you preview again. The apply is recorded in the audit log as `billing.tax.pack_applied` with the full diff.
6. Set the tax class of your products. Products are never reassigned automatically. A product with no class is `standard`; the packs give `standard` and `digital_service` the same rates, but set `digital_service` on what you sell as electronically supplied services so the intent is explicit (owner question 8).

The tax engine uses pack and admin rules only when `billing_tax_enabled` is on and, in production, after the tax engine is switched to `v2` (design section 5).

## 3. Updating a pack

A new Rackflow release may ship a new version of a built-in pack, for example after a member state changes its rate. Rackflow never applies it for you. Preview the new version with an effective from date (normally the date the new rate takes effect, which must be in the future when you apply):

- A changed rule closes the old row at the effective from date (`valid_to`) and inserts a new row from that date that records which row it supersedes. Invoices with an earlier tax point keep the old rate.
- A rule no longer in the pack is closed at the effective from date.
- A rule you disabled stays disabled in its superseding row.
- Rules you created yourself (source `admin`) and rules mirrored from the legacy tax rates are never touched.

If a rate change takes effect on a date that has already passed by the time you learn of it, apply from now and issue credit notes and new invoices for anything issued at the wrong rate in between.

## 4. VAT numbers

When a client enters a VAT number, Rackflow:

1. Normalises it (upper case; spaces, dots and dashes removed; the country prefix split off, `EL` for Greece, `XI` read as `GB`).
2. Checks its shape for the EU 27 and the UK. A number of the wrong shape is `format_invalid`. The shape check is not evidence that the number is registered.
3. Asks the first validator listed in `billing_tax_id_validators` that is enabled and supports the number, such as the VIES plugin. With no validator the status stays `unchecked`. UK numbers are only shape checked in phase 2; no UK rule depends on a valid UK number.
4. Stores every check as evidence (`tax_id_checks`), including the VIES consultation number when your requester VAT number is configured on the plugin, and reuses a result for a while: `valid` for `billing_tax_id_valid_days` (30 by default), `invalid` for one day, `unavailable` for 15 minutes.

Reverse charge applies only to a business whose number was found `valid` within `billing_tax_id_max_age_days` (30 by default) of the invoice's tax point. The `tax_id_recheck` job keeps active clients fresh once you turn it on.

**When VIES is down** a check is `unavailable`; a timeout, plugin error, open circuit or rate limit never counts as `invalid`. Setting `billing_tax_id_unavailable_policy` decides what checkout does:

- `charge_tax` (default): the client is charged VAT as if they had no valid number and sees "We could not verify your VAT number; VAT has been charged. We will retry." A later `valid` result applies to later invoices; to refund the earlier VAT, issue a credit note and a reverse charge invoice by hand.
- `block_checkout`: a checkout by a business with an EU VAT number whose status is `unavailable` is refused (`tax_id_unverifiable`) until the check succeeds.

Renewal invoices are never blocked by either policy.

## 5. Legal notes

The note texts are stored in `tax_legal_notes` per key and locale, seeded in English. You may edit them in the tax admin; a pack update never overwrites an edited text. The text is copied onto each invoice at publish, so an edit never changes an issued invoice. Placeholders are bare names in braces (`{reason}`, `{number}`, `{date}`); write `{{` and `}}` for literal braces. A note in a locale with no text falls back to its language (`en` for `en-GB`) and then to English.

## 6. Sources and what could not be confirmed

Checked on 22 November 2026:

- EU standard rates: European Commission, Your Europe, "VAT rules and rates" (<https://europa.eu/youreurope/business/taxation/vat/vat-rules-rates/index_en.htm>, page last checked by the Commission on 13 July 2026). All 27 rates in the pack match its table, including Estonia 24%, Finland 25.5%, Romania 21% and Slovakia 23%.
- UK rates: GOV.UK, "VAT rates" (<https://www.gov.uk/vat-rates>) and "VAT rates on different goods and services" (<https://www.gov.uk/guidance/rates-of-vat-on-different-goods-and-services>, last updated 10 July 2026): standard 20%, reduced 5%, zero 0%.
- Slovakia: Financial Administration of the Slovak Republic, "Sadzby dane" (<https://www.financnasprava.sk/sk/podnikatelia/dane/dan-z-pridanej-hodnoty/sadzby-dane>): standard 23%, reduced 19% and 5%. A secondary source listed a "VAT rates update" in Slovakia on 1 January 2027 without detail. The Financial Administration's information on the amendments in force from that date (Guide 1/DPH/2026/I of 14 January 2026, on Act No. 385/2025) describes e-invoicing, reporting and tax payment measures and no rate change, so the pack keeps 23%.
- ViDA: Council Directive (EU) 2025/516 on EUR-Lex (<https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32025L0516>), recital 36, Article 2 point (4) and Article 6(2), read on 22 November 2026 (section 1.4).
- Secondary sources only, not relied on: a VAT rates table updated in early November 2026 reported that no member state had voted a new standard rate for 2027.

Not confirmed:

- Rate changes scheduled after 22 November 2026 were not checked against the TEDB. Check before applying (section 2). For Slovakia, the absence of a rate change on 1 January 2027 rests on the Financial Administration's guide as summarised in its search listing; the guide itself was not read in full.
- The start dates of the current rates in the packs (`valid_from`, for example 2011-01-04 for UK 20%) come from general knowledge, not from the sources above. They have no effect: an apply always starts rules at its effective from date, which cannot be in the past.

## 7. Questions for your tax adviser

These are the assumptions the packs make (design section 18):

- Whether everything you sell (virtual machines, bare metal, proxies, storage, backup) is an electronically supplied service. Bare-metal rental or colocation could be hire of equipment or a service connected to property, with a different place of supply (question 8).
- For a UK seller: no UK VAT on digital services to consumers outside the UK; EU consumers need the non-union One Stop Shop registration, and without one Rackflow charges nothing and raises the `tax_not_registered` alert (question 9).
- The wording of the reverse charge and outside-the-scope notes (question 10).
- Northern Ireland treated as the UK for services (question 11).
- No UK domestic reverse charge for hosting (question 16).
- Rounding per invoice per rate, half up (question 1).
- Late fees outside the scope of VAT (question 3).
- For an EU seller: whether the seller-state rows (section 1.4) are right below the 10,000 euro threshold, and how the threshold is calculated from 1 January 2027 under ViDA (Directive 2025/516, Article 2(4)) as transposed in your member state.
