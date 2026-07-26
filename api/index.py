from vercel_wsgi import handle_wsgi
from config.wsgi import application

handler = handle_wsgi(application)
