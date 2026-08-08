

# IIcona — Conector de IA para 1C:Enterprise

> Extensión para integrar modelos de lenguaje (LLM) en configuraciones de 1C:Enterprise 8.3:
> conector unificado a proveedores, ciclo de agentes con herramientas, RAG, servidor MCP,
> monitoreo de errores con análisis de IA y generador de diagramas.

[![Licencia: MIT](https://img.shields.io/badge/Лицензия-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![1C:Enterprise](https://img.shields.io/badge/1С-8.3-blue)](https://1c.ru)
[![Versión](https://img.shields.io/badge/версия-1.3.0-green)](https://github.com/andromanpro/1c-ai-connector/releases)
[![Pruebas](https://img.shields.io/badge/YAxUnit-398-brightgreen)](#-calidad-del-código)

## 🎯 Sobre el proyecto

**IIcona (KII — Conector de Inteligencia Artificial)** — extensión para trabajar
con modelos de lenguaje directamente desde 1C. Comenzó como un conector de API con una interfaz unificada
para todos los proveedores populares; desde la versión 1.3.0 es una plataforma de subsistemas de IA:
sobre el conector funcionan el ciclo de agentes con herramientas, búsqueda semántica
en la base de conocimientos (RAG), servidor MCP para agentes de IA externos, monitoreo de errores
con diagnóstico de IA y generador de diagramas.

Se instala como una extensión de configuración, no modifica la configuración principal.

**Escenarios listos para usar:**

- 🚨 Monitoreo de errores de la base de datos: recopilación del registro, diagnóstico de IA, Telegram
- 🔍 Auditoría de código por error: ubicación en el código fuente + explicación de la causa
- 🛠 Base de datos 1C como conjunto de herramientas para Claude/Cursor (MCP)
- 📊 Diagramas a partir de descripción de texto (mermaid/plantuml/graphviz/bpmn)

**Y una API para sus propias soluciones:**

- 🤖 Chatbots para atención al cliente
- 📝 Generación de contenido (descripciones de productos, documentos)
- 🔍 Clasificación y extracción de datos estructurados de texto
- 📚 Respuestas basadas en su propia base de conocimientos (RAG)
- 🎯 Agentes que navegan autónomamente por los datos de 1C utilizando herramientas

## ✨ Funcionalidades

### Conector (núcleo)

- **Cinco formatos de proveedores** con conectores individuales: APIs compatibles con OpenAI,
  Anthropic, Google Gemini, YandexGPT, GigaChat, más de 25+ modelos listos
  en la plantilla de preconfiguraciones.
- **Solicitudes síncronas y asíncronas** (tareas en segundo plano), historial de mensajes,
  prompts del sistema.
- **Multimodalidad**: imágenes y documentos como adjuntos a la solicitud,
  generación de imágenes en la respuesta.
- **Caché de respuestas** con TTL (por modelo, opcional), registro completo de solicitudes
  (tokens, tiempo, errores), soporte para proxy, OAuth2 (GigaChat), IAM (Yandex).
- **Reintentos con retroceso exponencial (backoff)** — solo para códigos transitorios (429/5xx),
  sin duplicación de POST.

### Ciclo de agentes (function calling)

El modelo invoca herramientas de 1C y continúa el razonamiento con los resultados, en todos
los cinco formatos de proveedores. Límites de rondas y llamadas, deduplicación de invocaciones repetidas,
forzar una conclusión final, auditoría de cada llamada.

> Los adaptadores de formato de todos los proveedores están cubiertos por pruebas; la ejecución en vivo se realizó
> en modelos compatibles con OpenAI. Activen las banderas "Soporta herramientas" en los demás
> proveedores en las preconfiguraciones después de realizar pruebas en vivo con sus propias claves.

### RAG — Base de conocimientos

Categorías de conocimientos, segmentación automática de documentos en fragmentos, embeddings
con control de versiones. Los conocimientos se vinculan a expertos y se inyectan automáticamente en el prompt
del sistema. Dos backends de búsqueda: integrado (1C puro, sin dependencias)
y [Qdrant](https://qdrant.tech), con cola de sincronización y verificación en segundo plano.
Las políticas de egress no permiten que fragmentos confidenciales salgan de la base de datos.

### Servidor MCP

Servicio HTTP (JSON-RPC 2.0): agentes de IA externos interactúan con la base como un conjunto
de herramientas: metadatos, estructura de objetos, consultas de solo lectura, diagramas.
Basic Auth, limitación de frecuencia, lista blanca de solicitudes, ocultación de secretos, auditoría
con autolimpieza. ⚠ Publique hacia el exterior solo a través de HTTPS (reverse-proxy con TLS).

### Monitoreo de errores

Recopilación programada desde el registro, normalización y deduplicación, análisis por lotes
con IA y análisis profundo de agentes, notificaciones en Telegram con antispam,
reglas de ignorado, estadísticas, panel HTML. Más una herramienta de auditoría de código: ante un error
encuentra la ubicación en el código fuente y explica la causa. El auditor funciona sobre el analizador de terceros
[rlm-tools-bsl](https://github.com/Dach-Coin/rlm-tools-bsl) (MIT):
el índice estructural del código devuelve el método y el gráfico de llamadas según la línea del error; el shim REST es
un repositorio separado [1c-ai-connector-audit-shim](https://github.com/andromanpro/1c-ai-connector-audit-shim),
su instalación se describe en la guía.

### Generador de diagramas

Descripción en texto → diagrama (mermaid / plantuml / graphviz / bpmn). Mermaid
se renderiza localmente con la biblioteca integrada mermaid.js, sin servicios externos;
los demás formatos y el renderizado en servidor (PNG/SVG, herramienta MCP) se realizan a través de
[kroki](https://kroki.io). El modelo con herramientas lee por sí mismo los metadatos
de la configuración y dibuja atributos reales.

## 🌐 Proveedores compatibles

| Proveedor | Formato de API | Function calling | Estado |
|-----------|------------|------------------|--------|
| **OpenAI** (GPT-4/5.x) | OpenAI Compatible | ✅ | ✅ |
| **Anthropic** (Claude) | Anthropic Messages | ✅ formato listo | ✅ |
| **Google** (Gemini) | Google AI | ✅ formato listo | ✅ |
| **DeepSeek** | OpenAI Compatible | ✅ | ✅ |
| **GigaChat** (Sber) | OAuth2 + legacy functions | ✅ formato listo | ✅ |
| **Yandex** (YandexGPT) | Yandex Cloud | ✅ formato listo | ✅ |
| **Mistral / Qwen / Groq / Grok** | OpenAI Compatible | ✅ | ✅ |
| **Ollama / LM Studio / LiteLLM** (locales) | OpenAI Compatible | depende del modelo | ✅ |
| **OpenRouter / Together / Fireworks y otros** | OpenAI Compatible | ✅ | ✅ |

## 📦 Instalación

### Requisitos

- **1C:Enterprise 8.3.24+** — la versión 1.3.0 se desarrolló y probó
  en **8.3.27**
- **BSP (Biblioteca de Subsistemas Estándar) 3.1.10+** — se probó
  en la base de demostración BSP 3.1.11.392
- Conexión a internet para proveedores en la nube (los modelos locales funcionan offline)
- Opcional: [Qdrant](https://qdrant.tech) para búsqueda vectorial rápida,
  [kroki](https://kroki.io) para renderizado de diagramas,
  [rlm-tools-bsl](https://github.com/Dach-Coin/rlm-tools-bsl) para el auditor de código

> Las versiones 1.0.x también se verificaron en 1C:ERP 2.5.17. La versión 1.3.0 en ERP
> no se volvió a verificar — los informes sobre el funcionamiento en otras configuraciones son bienvenidos
> en [Issues](https://github.com/andromanpro/1c-ai-connector/issues).

### Pasos de instalación

1. Descargue el archivo `.cfe` desde [Releases](https://github.com/andromanpro/1c-ai-connector/releases)
2. Conecte la extensión (Administración → Extensiones de configuración)
3. Actualice la base de datos
4. Abra el catálogo "Modelos de IA" y haga clic en "Rellenar modelos": se cargarán
   más de 25 preconfiguraciones
5. Indique las claves API de los modelos necesarios (se almacenan en el almacenamiento seguro de 1C,
   no se incluyen en las exportaciones); el botón para probar la conexión está en el formulario del modelo
6. Cree un "Experto de IA": modelo + prompt del sistema
7. Los nuevos subsistemas se activan rellenando sus configuraciones: token de Telegram
   para el monitoreo, dirección de Qdrant para RAG, dirección de kroki para diagramas,
   usuario de API para MCP: lo que no se configure simplemente no funcionará,
   sin interferir con el resto

### Actualización desde 1.0.x

Estándar: los datos de modelos y expertos se conservan, la API pública es compatible
(`ЗапросКМодели`, `ЗапросКМоделиВФоне`, `ПараметрыМодели` — mismas firmas).
El único cambio de comportamiento: el rol "KII Permisos completos" ya no
otorga permisos sobre objetos de la configuración principal, solo sobre objetos
de la extensión (se cerró la concesión excesiva).

## 🚀 Inicio rápido

### Solicitud más simple (3 líneas de código)

```bsl
Модель = Справочники.КИИ_МоделиИИ.НайтиПоНаименованию("gpt-4");
Ответ = КИИ_КоннекторИИ.ЗапросКМодели(Модель, "Привет! Как дела?");

Если Не Ответ.ЭтоОшибка Тогда
    ОбщегоНазначения.СообщитьПользователю(Ответ.ТекстОтвета);
КонецЕсли;
```

### Con prompt del sistema e historial

```bsl
Промпт = Новый Структура;
Промпт.Вставить("СистемныйПромпт", "Ты — помощник программиста 1С");
Промпт.Вставить("ПользовательскийПромпт", "Как создать новый документ?");
Промпт.Вставить("ИсторияСообщений", История); // opcional

Ответ = КИИ_КоннекторИИ.ЗапросКМодели(Модель, Промпт);
```

### Solicitud asíncrona (tarea en segundo plano)

```bsl
ДлительнаяОперация = КИИ_КоннекторИИ.ЗапросКМоделиВФоне(Модель, Промпт);
ДлительныеОперации.ОжидатьЗавершение(ДлительнаяОперация);
Ответ = ПолучитьИзВременногоХранилища(ДлительнаяОперация.АдресРезультата);
```

### Markdown → HTML

```bsl
HTML = КИИ_МаркдаунПарсерКлиентСервер.ПреобразоватьВHTML(Ответ.ТекстОтвета);
```

### Pruebas sin código

El procesamiento **"Prueba IA"**: chat con un experto, adjuntos (imágenes/documentos),
historial de diálogo, visualización de registros: todo es interactivo.

## 📚 Documentación y desarrollo

- **Guía del usuario (wiki)** — [1c-ai-connector-guide](https://github.com/andromanpro/1c-ai-connector-guide):
  configuración de modelos y proveedores, RAG y base de conocimientos, monitoreo de errores
  y auditor de código, servidor MCP y seguridad, generador de diagramas.
- **Pruebas automáticas (398 pruebas YAxUnit)** — [1c-ai-connector-tests](https://github.com/andromanpro/1c-ai-connector-tests);
  para ejecutarlas se necesita [YAxUnit](https://github.com/bia-technologies/yaxunit) en la base
  y la extensión de pruebas.
- **Proxy MCP para clientes stdio** — [1c-ai-connector-mcp-proxy](https://github.com/andromanpro/1c-ai-connector-mcp-proxy)
  (Claude Desktop y otros clientes sin transporte HTTP).
- **Shim de auditoría** — [1c-ai-connector-audit-shim](https://github.com/andromanpro/1c-ai-connector-audit-shim):
  puente REST del auditor de código hacia el analizador rlm-tools-bsl.

## 🔑 Obtención de claves API

<details>
<summary>Instrucciones por proveedor</summary>

### OpenAI
1. [platform.openai.com](https://platform.openai.com) → API keys → cree una clave
2. Recargue su saldo

### Anthropic (Claude)
1. [console.anthropic.com](https://console.anthropic.com) → API Keys

### Google (Gemini)
1. [Google AI Studio](https://aistudio.google.com) → Get API key

### DeepSeek
1. [platform.deepseek.com](https://platform.deepseek.com) → API Keys

### GigaChat (Sber)
1. [developers.sber.ru/gigachat](https://developers.sber.ru/gigachat)
2. Obtenga una Authorization key (o ClientId:ClientSecret), el módulo intercambiará automáticamente
   por un token OAuth2

### Yandex GPT
1. [Yandex Cloud](https://console.cloud.yandex.ru) → cuenta de servicio → clave API
2. En el modelo, indique el Folder ID

</details>

## 🔍 Calidad del código

- ✅ **398 pruebas unitarias** (YAxUnit): conectores, ciclo de agentes, RAG,
  monitoreo, MCP, analizadores
- ✅ **SonarQube (BSL Language Server): 0 advertencias**, calificaciones A/A,
  deuda técnica 0; las excepciones conscientes están documentadas con supresiones
  con justificación directamente en el código
- ✅ Pasa las verificaciones de certificación de EDT (documentación, dominios, roles)
- ✅ Probado en la base de demostración BSP 3.1.11.392 / plataforma 8.3.27

## 🗺️ Hoja de ruta (Roadmap)

### Completado (v1.3.0)

- [x] Vision API — imágenes y documentos como adjuntos + generación de imágenes
- [x] Mejora en el manejo de límites de velocidad (rate limits) — backoff, reintento solo para códigos transitorios
- [x] Function calling — ciclo de agentes en los cinco formatos de proveedores
- [x] API de Embeddings — y todo el RAG sobre él
- [x] Más allá del plan: servidor MCP, monitoreo de errores con análisis de IA, auditor de código,
  generador de diagramas, 397 pruebas automáticas

### Planes

- [ ] Streaming — no es posible en BSL puro (la plataforma devuelve la respuesta HTTP completa);
  se está investigando una emulación mediante una tarea en segundo plano con sondeo
- [ ] Pruebas en vivo (smoke) de function calling en Anthropic/Google/Yandex/GigaChat
  y activación de banderas en las preconfiguraciones
- [ ] CI: compilación de `.cfe`, ejecución de pruebas y Sonar en cada commit
- [ ] Ampliación de la cobertura de pruebas (medición de cobertura con herramientas)

## ❓ Preguntas frecuentes (FAQ)

<details>
<summary>Preguntas frecuentes</summary>

### ¿Qué plataforma de 1C es compatible?

8.3.24+; la versión 1.3.0 se desarrolló y probó en 8.3.27.

### ¿Es necesaria la BSP?

Sí, BSP 3.1.10+ (se probó en 3.1.11.392).

### ¿Es compatible el envío de imágenes?

Sí, desde la versión 1.3.0: las imágenes y documentos se adjuntan a la solicitud
(en "Prueba IA" — pestaña de adjuntos), también se soporta la generación de imágenes.

### ¿Se puede usar sin internet?

Sí — modelos locales a través de Ollama/LM Studio, la búsqueda RAG integrada
y el renderizado local de diagramas mermaid también funcionan sin servicios externos.

### ¿Dónde se almacenan las claves API?

En el almacenamiento seguro de 1C; no se incluyen en las exportaciones de configuración.

### ¿Cómo controlar los costos?

Todas las solicitudes se registran en un registro con tokens y tiempo; el caché de respuestas
reduce las llamadas repetidas.

### ¿Se puede agregar un proveedor personalizado?

Cualquier API compatible con OpenAI funciona directamente: cree un modelo, indique
la dirección, el formato `OpenAI_Compatible`, el nombre del modelo y la clave.

### ¿Se puede usar en proyectos comerciales?

Sí, licencia MIT.

</details>

## 🤝 Participación en el proyecto

- 🐛 **¿Encontró un error?** — [Issue](https://github.com/andromanpro/1c-ai-connector/issues)
- 💡 **¿Tiene una idea?** — [Discussions](https://github.com/andromanpro/1c-ai-connector/discussions)
- 🔧 **¿Quiere ayudar?** — Pull Request

## 🙏 Agradecimientos

- [YAxUnit](https://github.com/bia-technologies/yaxunit) — motor de pruebas unitarias para 1C
- [rlm-tools-bsl](https://github.com/Dach-Coin/rlm-tools-bsl) — análisis semántico de BSL, sobre el cual funciona el auditor de código
- [kroki](https://kroki.io) y [mermaid](https://mermaid.js.org) — renderizado de diagramas
- [Qdrant](https://qdrant.tech) — búsqueda vectorial
- **OpenAI, Anthropic, Google, Yandex, Sber** — por las APIs
- **A la comunidad de 1C** — por el soporte y las ideas

## 📄 Licencia

[MIT](LICENSE) © 2026 Román Andriyánov ([androman.pro](https://androman.pro))

## 📞 Contacto

- 🌐 **Sitio web**: [androman.pro](https://androman.pro)
- 💬 **Telegram**: [@andromanpro1c](https://t.me/andromanpro1c)
- 📧 **Email**: andromanpro@gmail.com

---

<div align="center">

**Hecho con ❤️ para la comunidad de 1C**

[⭐ Poner estrella](https://github.com/andromanpro/1c-ai-connector) • [💬 Discusiones](https://github.com/andromanpro/1c-ai-connector/discussions) • [🐛 Reportar error](https://github.com/andromanpro/1c-ai-connector/issues)

</div>
