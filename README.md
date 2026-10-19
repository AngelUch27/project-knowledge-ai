# Project Knowledge AI

Demo privado de preguntas y respuestas sobre documentos, con FastAPI, Agent Search y Gemini. La interfaz web se sirve desde la propia API.

## Secciones del demo

- **Chat:** pregunta en español, respuesta de Gemini y títulos de las fuentes recuperadas.
- **Documentos:** carga PDF, DOCX o XLSX; muestra archivos del bucket y el estado de importación.
- **Actividad:** muestra cargas/importaciones recientes y mensajes de estado.

## Preparar una computadora nueva

Requisitos: Windows PowerShell, Git, Python 3.10 o posterior y Google Cloud CLI (`gcloud`). La persona necesita acceso al repositorio y permisos sobre los recursos del proyecto de GCP; tener acceso de lectura al repositorio o al proyecto por sí solo no concede esos permisos.

Clona la rama que contiene esta versión y entra a la carpeta:

```powershell
git clone --branch feat/agent-search-rag https://github.com/AngelUch27/project-knowledge-ai.git
Set-Location project-knowledge-ai
```

## Ejecutar la aplicación localmente

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
Copy-Item .env.example .env
```

`.env.example` ya contiene los IDs del proyecto y sus recursos del demo. `.env` es la configuración local y está excluido de Git. No agregues credenciales ni archivos de claves.

El administrador de GCP debe dar a cada persona que ejecutará la aplicación permisos para consultar Agent Search (`roles/discoveryengine.user`), usar Vertex AI (`roles/aiplatform.user`) y leer, listar, cargar y actualizar objetos del bucket (`roles/storage.objectUser`). Después, autentícate con tu propia cuenta, sin crear ni descargar claves:

```powershell
gcloud auth login
gcloud config set project geglobal-level-program
gcloud auth application-default login
```

Inicia la aplicación en el equipo local:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Abre `http://127.0.0.1:8000`. Al enlazarse a `127.0.0.1`, la API solo acepta conexiones de esta computadora; además, valida el host y rechaza solicitudes de navegador de otros orígenes. La web y la API comparten origen, así que no se habilita CORS ni se expone una API pública. No cambies el host a `0.0.0.0` para este demo. Si luego se publica, usa Cloud Run con autenticación IAM y una cuenta de servicio adjunta al runtime; no despliegues una API pública sin una capa de autenticación.

## Carga e indexación automática

La pestaña Documentos carga al bucket `knowledge-agent-cap` bajo `documents/raw/`. También se vigilan otros prefijos del bucket. Para cada objeto compatible finalizado, una función de Cloud Run invocada por Eventarc llama a `ImportDocuments` con la URI exacta de ese objeto y modo incremental. Se admiten PDF, DOCX, PPTX, XLSX, XLSM, TXT, MD, HTML y JSON. El estado queda en metadatos del objeto y aparece en Documentos y Actividad. Los archivos de otros tipos se ignoran.

## Desplegar la ingesta automática en GCP

Esto se ejecuta una sola vez por proyecto, desde la raíz del repositorio. La cuenta que lo despliegue necesita permisos para administrar IAM, habilitar servicios y desplegar Cloud Run Functions/Eventarc. En particular, requiere `roles/run.sourceDeveloper`, `roles/serviceusage.serviceUsageConsumer` y `roles/iam.serviceAccountUser` sobre la cuenta de ejecución; también debe poder crear cuentas y roles de servicio y modificar políticas IAM. Un administrador del proyecto puede ejecutar estos pasos con su propia cuenta. Consulta los permisos de [despliegue de funciones](https://cloud.google.com/run/docs/deploy-functions).

En PowerShell, inicia sesión, selecciona el proyecto y concede a la cuenta activa el permiso para usar la identidad de ejecución. El bloque crea esa cuenta si aún no existe:

```powershell
gcloud auth login
gcloud config set project geglobal-level-program
$project = "geglobal-level-program"
$serviceAccount = "knowledge-agent-ingest@$project.iam.gserviceaccount.com"
$activeAccount = (gcloud auth list --filter="status:ACTIVE" --format="value(account)").Trim()
gcloud iam service-accounts describe $serviceAccount --project $project *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud iam service-accounts create knowledge-agent-ingest --project $project --display-name "Knowledge Agent document ingestion"
}
gcloud iam service-accounts add-iam-policy-binding $serviceAccount --member="user:$activeAccount" --role="roles/iam.serviceAccountUser" --project $project
.\scripts\deploy-ingestion.ps1
gcloud functions describe knowledge-agent-gcs-import --gen2 --region us-central1 --project $project --format="value(state)"
```

El último comando debe mostrar `ACTIVE`. El script habilita APIs y despliega `knowledge-agent-gcs-import` usando una cuenta de servicio **sin clave**. Limita la importación de Agent Search al data store indicado y la actualización de metadatos al bucket. Si la política de la organización lo requiere, un administrador también debe conceder `roles/run.builder` a la cuenta de servicio de Cloud Build indicada por Google Cloud.

Si estos comandos devuelven `PERMISSION_DENIED`, la cuenta activa no tiene permisos suficientes: un administrador del proyecto debe concederlos. La persona que ejecuta la aplicación local no necesita permisos de despliegue.

Las cargas producen un evento de Cloud Storage; Eventarc y Cloud Run Functions vuelven a intentar entregas fallidas. La importación es incremental y se solicita por archivo. Agent Search procesa e indexa de forma asíncrona, por lo que el disparador automatiza el envío pero un documento puede tardar varios minutos en estar disponible en búsquedas. La pestaña muestra “Enviado a Agent Search” cuando termina la operación de importación; eso no afirma que la propagación de búsqueda ya concluyó.

Los objetos que ya existían antes de crear el disparador no generan eventos retroactivos. Si se necesita sincronizar el histórico, se hace una importación inicial separada; no se ejecuta automáticamente para evitar volver a ingerir contenido existente.

## Rutas y configuración

- `GET /health`: estado local del servidor; no prueba conectividad con GCP.
- `GET /documents`: lista archivos compatibles del bucket y sus metadatos de importación.
- `POST /documents`: carga un archivo al bucket.
- `POST /search`: consulta la Search App.
- `POST /ask`: busca contexto y genera una respuesta con Gemini.
- `/docs`: documentación interactiva de FastAPI (solo accesible localmente).

Variables en `.env`: `GCP_PROJECT_ID`, `GCP_LOCATION`, `GCS_BUCKET`, `GEMINI_MODEL`, `AGENT_SEARCH_LOCATION`, `AGENT_SEARCH_APP_ID`, `AGENT_SEARCH_SERVING_CONFIG` y `AGENT_SEARCH_DATA_STORE_ID`. ADC aporta las credenciales. No guardes tokens, claves ni documentos en Git.

Las operaciones de búsqueda, importación y Gemini pueden generar cargos en el proyecto compartido.
