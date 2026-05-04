# Refferal-bot

Shopify site checker and card testing tool.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python Test.py
```

The tool provides two modes:

- **Checker** -- test cards against Shopify sites (single or mass).
- **Site** -- verify whether Shopify sites are alive (single or mass).

## Project Structure

| File | Description |
|---|---|
| `Test.py` | Main CLI entry-point with checker and site menus |
| `api.py` | Gateway API helpers (`process_card`, `parse_cc_string`, etc.) |
| `requirements.txt` | Python dependencies |
