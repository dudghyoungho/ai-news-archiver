# 🤖 Smart Link Archiver (AI-Powered News Platform)

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-5.0-092E20?logo=django&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![AWS](https://img.shields.io/badge/AWS-Lightsail-232F3E?logo=amazon-aws&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-Async_Queue-37814A?logo=celery&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?logo=postgresql&logoColor=white)

> **A smart news archiving platform that scrapes, summarizes, and recommends articles using AI.** > Deployed on AWS Lightsail with a fully containerized architecture.

---

## 📸 Demo & Screenshots

| **Web Dashboard** | **Chrome Extension** |
|:---:|:---:|
| | |
| *Managing archived links with AI summaries* | *One-click save & summarize* |

---

## 🚀 Key Features

* **📰 AI-Powered Summarization:** Automatically summarizes news content using OpenAI API.
* **⚡ Asynchronous Processing:** Decoupled AI tasks using **Celery & Redis**, reducing user wait time from **4s to under 0.2s**.
* **🔍 Vector Search (RAG):** Implemented semantic search using **pgvector** to recommend related articles based on context, not just keywords.
* **🧩 Chrome Extension Integration:** Developed a browser extension with JWT authentication for seamless link saving.
* **📱 Server-Driven UI:** Utilized **HTMX** for SPA-like interactivity without complex frontend frameworks.

---

## 🏗️ System Architecture

This project adopts a **Micro-service oriented Monolith** architecture to ensure scalability and maintainability.

```mermaid
graph TD
    Client[Client (Web/Extension)] -->|HTTP Request| Nginx
    Nginx -->|Reverse Proxy| Web(Django + Gunicorn)
    
    subgraph Docker Network
        Web -->|Task Push| Redis
        Web -->|Read/Write| DB[(PostgreSQL + pgvector)]
        
        Worker[Celery Worker] -->|Task Pop| Redis
        Worker -->|Save Result| DB
        Worker -->|API Call| OpenAI[OpenAI API]
    end
