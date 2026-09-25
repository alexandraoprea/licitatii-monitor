# Monitor de licitații — RomGreen

Serviciul urmărește licitațiile relevante din SEAP și DataDriven și trimite un singur e-mail agregat către destinatarii configurați în dashboard.

## Ce face

- Interoghează sursa publică SEAP pentru **anunțuri de participare** aflate în desfășurare și proiectul monitorizat din DataDriven.
- Filtrează după cele 16 coduri CPV configurate.
- Reține într-o bază SQLite mică anunțurile deja procesate, astfel încât fiecare anunț este trimis o singură dată.
- La prima rulare pentru fiecare sursă își creează reperul fără să trimită retrospectiv e-mailuri. Rulările următoare trimit numai noutățile.
- E-mailul are secțiuni separate pentru SEAP și DataDriven și conține: număr anunț, obiect, autoritate, CPV, termen de depunere și link direct.

## Pornire

1. Copiază `.env.example` ca `.env` și completează datele SMTP ale expeditorului. Pentru Gmail se poate folosi `smtp.gmail.com`, port `465`, `SMTP_SECURITY=ssl` și o App Password. Parola nu se păstrează în cod sau în Git.
2. Creează fișierul indicat de `DATADRIVEN_SECRET`, cu liniile `USERNAME=...` și `PASSWORD=...`. Fișierul trebuie protejat și exclus din Git.
3. Alege `DASHBOARD_PASSWORD` în `.env`, apoi pornește serviciul:

   ```sh
   docker compose up -d --build
   ```

4. Deschide dashboardul la `http://IP-SERVER:8080` și autentifică-te cu utilizatorul/parola din `.env`. Acolo se configurează destinatarii, se vede ultima rulare și fiecare e-mail trimis, inclusiv sursa acestuia.

Verificarea rulează automat la pornire și apoi la fiecare oră. Pentru instalare directă, fără Docker, este suficient Python 3.6+ și comanda `python3 dashboard.py`, cu aceleași variabile din `.env` exportate în mediu.

## Observații operaționale

- Intervalul este o dată pe oră. Fereastra de 72 de ore asigură că o repornire temporară nu pierde anunțuri.
- Soluția folosește endpointul public SEAP verificat la 25 septembrie 2026. Dacă SEAP îi modifică interfața publică, endpointul poate necesita ajustare.
- Codurile sunt filtrate după CPV-ul disponibil în fiecare sursă. Pentru SEAP acesta este CPV-ul principal din lista publică.
- Nu este nevoie de o bază de date separată: tot istoricul și setările sunt păstrate local într-un singur fișier SQLite din `data/`.
