# Apagones Worker

Worker en GitHub Actions que procesa mensajes de Telegram sobre apagones en Venezuela.

## Setup

1. Configura los secrets en GitHub (Settings → Secrets and variables → Actions)
2. Añade `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `ANTHROPIC_API_KEY` (opcional)
3. El workflow se ejecuta automáticamente cuando llegan mensajes

## Desarrollo local

```bash
pip install -r requirements.txt
python ingest.py --dry
```
