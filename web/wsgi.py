"""
wsgi.py — Entry point para PythonAnywhere.
En el panel de PA configurás: Source: /home/TUUSUARIO/mailer/web/wsgi.py
"""
import sys, os

# Ajustar path al proyecto
project_home = os.path.expanduser("~/mailer")
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Cargar variables de entorno desde .env
from dotenv import load_dotenv
load_dotenv(os.path.join(project_home, ".env"))

from web.app import app as application
