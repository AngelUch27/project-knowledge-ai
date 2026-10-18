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

## Dependencias y configuración

Las dependencias de Python están en `backend/requirements.txt`. Para reinstalarlas después de actualizar ese archivo, ejecuta:

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

No se incluyen credenciales, claves ADC ni configuración de GCP en esta etapa. No guardes secretos en el repositorio.
