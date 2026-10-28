# Karigar Ledger

A Streamlit application for managing a garment workshop ledger, including daily work entries, kharcha (cash advances), karigar settlements, rate management, and PDF/WhatsApp exports.

## Features

- Daily work entry by date and karigar
- Item tracking with automatic creation of new items
- Kharcha / advance cash logs
- Settlement reports for the last 7/14 days or custom ranges
- Current item rate master
- Export to PDF
- WhatsApp-ready settlement text
- Karigar management with activate/deactivate support
- Local SQLite database backup/restore
- PIN protection for Mamu's Hisaab section

## Tech Stack

- Python
- Streamlit
- SQLite
- pandas
- fpdf2

## Project Structure

- `app.py` — main Streamlit application
- `requirements.txt` — Python dependencies
- `workshop_ledger.db` — SQLite database generated automatically on first run

## Requirements

- Python 3.10+
- pip

## Setup

1. Clone the repo
2. Create a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

   On Windows:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Run the app:

   ```bash
   streamlit run app.py
   ```

## Default Settings

- Default Mamu PIN: `1234`
- Database file: `workshop_ledger.db` created in the project root
- Seed items are added automatically on first run

## Secure PIN Setup

For production or shared deployments, set a secure PIN in Streamlit secrets.

Create a `.streamlit/secrets.toml` file:

```toml
MAMU_PIN = "5678"
```

Then run the app normally. If the secrets file is not present, the app falls back to `1234`.

## Notes

- The app is designed for Indian workshop use and includes rupee formatting.
- It uses IST (UTC+5:30) for daily date logic so the app behaves correctly on hosted servers running in UTC.
- The PDF exporter uses built-in PDF fonts, so some non-Latin characters may not display perfectly.

## Backup Recommendation

Because the app stores data in a local SQLite file, regular backups are strongly recommended.

Use the built-in backup feature from the Karigar Master tab, or copy the database file manually.

## Running on Streamlit Cloud

This app is compatible with Streamlit Community Cloud.

1. Push the repository to GitHub
2. Create a new Streamlit app in Streamlit Cloud
3. Set the app main file to `app.py`
4. Add a secrets file with `MAMU_PIN` if needed
5. Deploy

## License

This project is provided as-is for workshop tracking use. Update this section if you intend to publish it under a specific license.
