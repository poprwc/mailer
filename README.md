# Campaign Mailer v3.0.0 — PythonAnywhere

App web de email marketing. Hosting gratuito en PythonAnywhere, sin tarjeta.

## Deploy en PythonAnywhere (10 minutos)

### 1. Crear cuenta
Ir a https://www.pythonanywhere.com → Register → Free account (Beginner)

### 2. Subir el código
En el panel de PA → Files → Upload a zip file → subir mailer_pa.zip
Luego abrir una Bash console y descomprimir:
```bash
cd ~
unzip mailer_pa.zip
mv mailer_pa mailer
```

### 3. Instalar dependencias
En la Bash console:
```bash
cd ~/mailer
pip3.10 install --user flask python-dotenv gunicorn
```

### 4. Crear archivo .env
```bash
cp .env.example .env
nano .env
```
Editar con tus valores:
```
SECRET_KEY=pon_aqui_algo_largo_y_aleatorio
CRON_TOKEN=pon_aqui_otro_token_secreto
DATABASE_URL=
```
Guardar: Ctrl+X → Y → Enter

### 5. Inicializar la base de datos
```bash
cd ~/mailer
python3.10 -c "from shared.db import init_db; init_db(); print('DB OK')"
```

### 6. Configurar Web App
- Panel PA → Web → Add a new web app
- Next → Flask → Python 3.10 → Next
- Path: /home/TUUSUARIO/mailer/web/wsgi.py (reemplazar TUUSUARIO)
- Click en el link del archivo WSGI que PA crea automáticamente
- Reemplazar TODO el contenido con el de web/wsgi.py de este proyecto
- Reload

### 7. Tu URL pública
https://TUUSUARIO.pythonanywhere.com
Login: admin / admin123 (cambiarlo en Configuración)

### 8. Configurar envío automático
Dos opciones (usar ambas para mayor confiabilidad):

**Opción A — cron-job.org (cada 5 min, recomendado)**
- Registrarse en https://cron-job.org (gratis)
- Create cronjob → URL: la que aparece en Configuración → "Configurar cron-job.org"
- Schedule: Every 5 minutes → Save

**Opción B — Cron PA (cada hora, backup)**
- Panel PA → Tasks → Add a scheduled task
- Command: python3.10 /home/TUUSUARIO/mailer/worker/cron.py
- Hour: cada hora

Con ambas activas: cron-job.org envía cada 5 min, PA hace backup cada hora.

### 9. Configurar SMTP
En la app → Configuración → SMTP

**Gmail:**
- Host: smtp.gmail.com / Puerto: 587
- Activar: myaccount.google.com → Seguridad → Contraseñas de aplicaciones
- Usuario: tu@gmail.com / Pass: la app password de 16 caracteres

**SendGrid (mejor para volumen):**
- Host: smtp.sendgrid.net / Puerto: 587
- Usuario: apikey / Pass: tu API key SG.xxx

## Estructura
```
mailer/
├── .env                  ← tus credenciales (no subir a GitHub)
├── .env.example
├── mailer.db             ← SQLite, se crea automáticamente
├── requirements.txt
├── shared/db.py          ← capa de datos
├── worker/cron.py        ← script de envío (llamado por cron)
└── web/
    ├── app.py            ← Flask app
    ├── wsgi.py           ← entry point para PA
    └── templates/
```
