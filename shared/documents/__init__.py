"""Trade documents: one design, one source of facts, for every document PortLess issues.

``render.py``  — the Jinja environment (service templates + the shared layout), filters,
                 and ``render_document()`` which marks a document DRAFT when a legally
                 required field is missing instead of printing a placeholder.
``issuer.py``  — PortLess as exporter of record (name, address, IEC, GSTIN, PAN, LUT, bank)
                 from the environment; nothing invented.
``facts.py``   — everything already true about a deal (PI, PO, supplier commitment,
                 inspector's loading record, freight booking, escrow, payouts), so a
                 document never falls back to a guess when the real figure exists.
"""
