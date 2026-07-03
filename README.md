# Apagones Worker

Worker en GitHub Actions que procesa los partes de la Empresa Eléctrica de La Habana (Cuba) desde su canal público de Telegram y los deposita normalizados en Supabase. Forma parte de la app Apagón.

## Setup

1. Configura los secrets en GitHub (Settings → Secrets and variables → Actions)
2. Añade SUPABASE_URL, SUPABASE_SERVICE_KEY y ANTHROPIC_API_KEY (opcional)
3. El workflow corre cada ~5 minutos y también a mano desde la pestaña Actions (Run workflow)

## Desarrollo local

```bash
pip install -r worker/requirements.txt
python worker/ingest.py --dry   # prueba sin Supabase
```
