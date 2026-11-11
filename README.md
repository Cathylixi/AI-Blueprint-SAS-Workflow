# Clinical Blueprint — SPI-611 MVP

**Live application (Azure):** [Open Clinical Blueprint](https://clinical-blueprint-mvp.delightfuldesert-f5bbaa56.eastus.azurecontainerapps.io/)

Sign in with your authorized company Microsoft account. The Azure application
includes the AI Chatbot and saves approved relationship changes to the database.

**Static demo (GitHub Pages):** [Open static preview](https://cathylixi.github.io/AI-Blueprint-SAS-Workflow/)

Interactive traceability prototype connecting protocol sections, aCRF fields,
raw metadata, SDTM annotations, ADaM variables, SAP rules, and output shells.

The catalog contains all 1,043 metadata records across 43 datasets (354 unique
variable names). Verified links, candidate mappings, and missing links are shown
separately so metadata presence is not mistaken for end-to-end traceability.

The static website is in `dist/`. Pushes to `main` deploy it through GitHub Pages.

## General PDF table reconstruction

`tools/pdf_to_structured_content.py` converts any source PDF into the shared
page-content schema used by the SAP modal. It first reads ruled cell boundaries,
then falls back to X/Y coordinate clustering for borderless tables. The process
also handles rotated pages, wrapped cells, repeated headers/footers, table
continuations, paragraphs, lists, and notes without page- or table-specific
rules.

The SAP source modal renders the generated pages as one continuous document.
Opening a SAP section automatically scrolls to its source page; the remaining
pages stay directly above and below it for normal vertical scrolling. The modal,
page cards, and reconstructed tables resize with the available browser viewport.

```powershell
python tools/pdf_to_structured_content.py input.pdf dist/sap_content.json `
  --document-id "study-sap"
```
