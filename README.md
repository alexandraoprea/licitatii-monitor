# Monitor de licitații SEAP — RomGreen

Serviciul urmărește anunțurile de participare publicate în SEAP și trimite un e-mail agregat către destinatarii configurați în dashboard, când găsește o licitație nouă cu unul dintre cele 16 coduri CPV configurate (curățenie, salubrizare/deșeuri, DDD și pază).

## Ce face

- Interoghează sursa publică SEAP pentru **anunțuri de participare** aflate în desfășurare; nu urmărește anunțuri de atribuire.
- Filtrează după CPV-ul principal publicat în lista SEAP.
- Reține într-o bază SQLite mică anunțurile deja procesate, astfel încât fiecare anunț este trimis o singură dată.
- La prima rulare își creează reperul fără să trimită retrospectiv e-mailuri. Rulările următoare trimit numai noutățile.
- E-mailul conține: număr anunț, obiect, autoritate, CPV, termen de depunere și link direct către SEAP.

## Pornire

1. Copiază `.env.example` ca `.env` și completează datele SMTP ale expeditorului. Pentru Gmail se poate folosi `smtp.gmail.com`, port `465`, `SMTP_SECURITY=ssl` și o App Password. Parola nu se păstrează în cod sau în Git.
2. Alege `DASHBOARD_PASSWORD` în `.env`, apoi pornește serviciul:

   ```sh
   docker compose up -d --build
   ```

3. Deschide dashboardul la `http://IP-SERVER:8080` și autentifică-te cu utilizatorul/parola din `.env`. Acolo se configurează destinatarii, se vede ultima rulare și fiecare e-mail trimis.

Verificarea rulează automat la pornire și apoi la fiecare oră. Pentru instalare directă, fără Docker, este suficient Python 3.6+ și comanda `python3 dashboard.py`, cu aceleași variabile din `.env` exportate în mediu.

## Observații operaționale

- Intervalul este o dată pe oră. Fereastra de 72 de ore asigură că o repornire temporară nu pierde anunțuri.
- Soluția folosește endpointul public SEAP verificat la 25 septembrie 2026. Dacă SEAP îi modifică interfața publică, endpointul poate necesita ajustare.
- Codurile sunt filtrate din CPV-ul principal disponibil în lista publică. Pentru o etapă ulterioară putem adăuga verificarea CPV-urilor secundare și DataDriven.
- Nu este nevoie de o bază de date separată: tot istoricul și setările sunt păstrate local într-un singur fișier SQLite din `data/`.
