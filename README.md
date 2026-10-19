# Project Knowledge AI

Backend inicial para el demo Project Knowledge AI de Capgemini. Este repositorio contiene solo el backend; el frontend se agregará en una carpeta independiente cuando definamos su tecnología.

## Requisitos

- Python 3.10 o posterior
- Git
- VS Code (opcional)

## Preparar otra computadora con Windows

1. Instala Python y Git con las herramientas permitidas por tu organización. Verifica que el comando `py` funcione:

   ```powershell
   py --version
   git --version
   ```

2. Clona el repositorio y entra a la carpeta:

   ```powershell
   git clone https://github.com/AngelUch27/project-knowledge-ai.git
   cd project-knowledge-ai
   ```

3. Crea un entorno virtual local e instala las dependencias declaradas en el proyecto:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
   ```

No se necesita activar el entorno virtual, lo que evita problemas con la política de scripts de PowerShell. Cada computadora crea su propio `.venv`; esa carpeta está excluida de Git y no se debe copiar entre equipos.

En VS Code, abre la carpeta clonada y selecciona `.venv\Scripts\python.exe` como intérprete de Python.

## Iniciar la API

Desde la carpeta del repositorio:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```

La API queda en `http://127.0.0.1:8000`.

- Estado: `GET http://127.0.0.1:8000/health`
- Documentación interactiva: `http://127.0.0.1:8000/docs`

La respuesta de `/health` no consulta GCP ni requiere credenciales:

```json
{"status":"ok","service":"project-knowledge-ai-api"}
```

## Integración GCP inicial

La primera versión permite cargar archivos PDF/DOCX a un bucket existente de Cloud Storage y preguntar por el contenido usando Gemini en Vertex AI. El contenido consultado se envía al modelo. La extracción de texto y la selección de fragmentos son locales y básicas; PDF escaneado/OCR, búsqueda semántica, base de datos y gestión de usuarios quedan fuera de este MVP.

1. Copia `.env.example` como `.env` y completa `GCP_PROJECT_ID` y `GCS_BUCKET` con valores de un proyecto y bucket ya aprobados. `GCP_LOCATION` y `GEMINI_MODEL` se pueden ajustar.
2. Configura ADC en el equipo siguiendo la guía oficial de autenticación de Google Cloud. No guardes archivos de claves ni tokens en el repositorio.
3. Habilita las APIs necesarias y concede a la identidad ADC permisos mínimos para Vertex AI y para crear/leer objetos en el bucket. No se crean recursos automáticamente.
4. Inicia Uvicorn desde la carpeta del repositorio. `/docs` muestra `POST /documents` y `POST /ask`.

Sin las variables de GCP, la API sigue arrancando y `/health` responde normalmente; las operaciones documentales indican qué configuración falta. El uso de Cloud Storage y Gemini puede generar cargos en la cuenta de GCP.

## Dependencias y configuración

Las dependencias de Python están en `backend/requirements.txt`. Para reinstalarlas después de actualizar ese archivo, ejecuta:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

No se incluyen credenciales ni claves ADC. `.env` está excluido de Git; no guardes secretos en el repositorio.
