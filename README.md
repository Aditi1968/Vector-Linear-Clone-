# Vector

A Linear-style issue tracker, built as a backend learning project.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then edit DATABASE_URL
export $(grep -v '^#' .env | xargs)
```
