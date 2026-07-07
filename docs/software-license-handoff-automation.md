# Software License Handoff Automation

## Goal

When a software license email arrives in Outlook, create a reviewed handoff record that can append the license to the managed Excel ledger, match the requester against cached approval data, and prepare a Teams message containing the installation file reference and license key.

## Safe Default

The first implementation must run in dry-run mode. It may parse email content, propose an Excel row, match an approval document, and draft a Teams message, but it must not send license keys to Teams or mutate the Excel workbook until an operator explicitly approves the run.

## Inputs

- Outlook or Microsoft 365 message payload from the Claude M365 connector.
- License email body and attachments.
- Target Excel workbook path and worksheet name.
- Existing asset/license number column used to calculate the next asset number.
- Approval cache from `GET /api/approvals` and `GET /api/approval-workflow?doc_id=<docId>`.
- Installation file catalog or fixed internal download link for each software product.
- Teams recipient identity, preferably the requester email or UPN resolved from approval data.

## Proposed Record

Each detected license email should produce one JSON receipt before any write:

```json
{
  "schema": "software-license-handoff.v1",
  "mode": "dry-run",
  "detected_at": "2026-05-14T00:00:00+09:00",
  "mail": {
    "message_id": "",
    "subject": "",
    "from": "",
    "received_at": ""
  },
  "license": {
    "software_name": "",
    "license_key_masked": "ABCD-****-****-WXYZ",
    "license_key_secret_ref": "",
    "order_id": "",
    "install_file_ref": ""
  },
  "excel_append": {
    "workbook": "",
    "worksheet": "",
    "next_asset_no": "",
    "row": {}
  },
  "approval_match": {
    "doc_id": "",
    "doc_title": "",
    "requester_name": "",
    "requester_upn": "",
    "confidence": "high|medium|low",
    "evidence": []
  },
  "teams_draft": {
    "recipient": "",
    "message_preview": "",
    "requires_operator_approval": true
  }
}
```

## Matching Rules

1. Normalize software names from the email subject, body, invoice/order line, and approval `contentsText`.
2. Prefer approval documents whose theme is `SW/라이선스/자산` and whose title or contents mention the detected software.
3. Prefer requester names from approval fields in this order: explicit 신청자 field from transformed detail content, `createdBy`, then text near `신청자명`.
4. Require a high-confidence match before Teams drafting includes the real license key.
5. If multiple approval documents match, stop at review-needed and list candidates.

## Excel Append Rules

1. Read the current workbook and worksheet with a structured spreadsheet library, not ad hoc CSV parsing.
2. Preserve existing formatting where possible.
3. Compute the next asset/license number from the configured column only.
4. Write masked license keys to ordinary receipts. Store full keys only in the workbook cell and Teams draft at the point of approved execution.
5. Save a before/after receipt containing row number, asset number, and non-secret fields.

## Teams Draft Rules

1. The dry-run message must mask the key.
2. The approved send message may include the full key only for the matched requester.
3. Installation files should be sent as approved internal links rather than copied binary attachments unless the user explicitly asks for file upload.
4. Every send must produce a receipt with message id, recipient, approval doc id, workbook row, and masked key.

## Unresolved Requirements

- Exact Excel workbook path, worksheet name, and column mapping.
- License email examples for each vendor or product.
- Install-file catalog path or mapping from software name to internal download link.
- Teams recipient resolution rule when the approval requester has no email/UPN in cached data.
- Whether approved execution should be run by Claude M365 connector, Microsoft Graph app credentials, or a local operator-confirmed command.

## Acceptance Criteria

- Dry-run can process a saved sample Outlook message payload without network writes.
- Dry-run outputs one receipt and one proposed Excel row.
- Approval matching cites exact `docId`, title, requester evidence, and confidence.
- Teams draft is generated but not sent until approval.
- Full license keys never appear in logs, stdout, or ordinary receipts.
