# SQLite WAL: runtime e atualização controlada

HotBot usa WAL apenas em URLs SQLite com arquivo. Cada conexão aplica busy_timeout=5000 ms e synchronous=FULL. URLs não SQLite e bancos em memória não recebem essa configuração. O processo continua suportado com um worker; WAL não permite vários escritores simultâneos e não garante ausência universal de SQLITE_BUSY.

## Pré-requisito obrigatório

A biblioteca efetivamente carregada por `sqlite3` precisa conter a correção WAL-reset: SQLite >=3.51.3, ou backports 3.44.6/3.50.7. Uma versão de Python por si só não prova isso. O aplicativo recusa iniciar SQLite com arquivo em bibliotecas antigas; não contorne a verificação.

Teste offline Windows desta mudança: Python 3.11.9, SQLAlchemy 2.0.31, aiosqlite 0.20.0 e SQLite 3.53.4. Foi criada uma venv independente e instalado requirements.txt nela. Apenas nessa área isolada, `_sqlite3.pyd` compatível com Python 3.11 x64 foi copiado do mesmo interpretador e colocado ao lado da sqlite3.dll x64 oficial 3.53.4, obtida em https://sqlite.org/2026/sqlite-dll-win-x64-3530400.zip. O diretório isolado foi adicionado na frente do PYTHONPATH; nenhuma DLL do Python global foi substituída. Não misture arquiteturas/ABIs e não copie essa DLL sobre um Python em execução. Antes de iniciar, confirme `sqlite3.sqlite_version` e `_sqlite3.__file__` no interpretador REAL do serviço. É necessário preparar e verificar o runtime isolado da VPS separadamente; essa execução não acessou a VPS.

Referência oficial: https://sqlite.org/wal.html#walresetbug

## Banco e backup

Use caminho absoluto de DATABASE_URL para arquivo local gravável, não armazenamento de rede. O diretório precisa permitir criar os arquivos auxiliares -wal e -shm. Não copie apenas o .db enquanto há escritores: transações já confirmadas podem estar no -wal. Nunca apague manualmente um WAL ativo.

Backup online seguro usa a API SQLite: abra origem/destino, execute `origem.backup(destino)`, feche ambos e valide `PRAGMA integrity_check` no destino. Isso foi exercitado com compras pagas, marcadores de entrega e duas reinicializações de esquema, preservando os registros. Para atualização real: pare o único serviço escritor, faça backup verificável do banco e configuração, prepare o runtime corrigido, substitua somente o código e reinicie no MESMO caminho absoluto. Verifique journal_mode=wal, synchronous=2 e busy_timeout=5000. A reversão exige código e banco compatíveis; não substitua o banco por uma cópia antiga descartando compras recentes.

## Limites

FULL protege a durabilidade de commits em WAL, sujeito às garantias reais do sistema de arquivos/hardware. Não foram introduzidos retries genéricos de checkout/entrega, chamadas de gateway sob lock SQL, nem polling de pagamento. A confirmação continua por webhook verificado. Testes locais com serviços falsos e relógio de agendamento comprimido não certificam capacidade da VPS nem limites Telegram/gateway.
