@echo off
chcp 65001 >nul
echo ========================================================
echo   SINCRONIZANDO REPOSITORIO COM O GITHUB (BRANCH MAIN)
echo ========================================================
cd /d "%~dp0"

git add -A
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERRO] Falha ao adicionar arquivos ao git stage.
    pause
    exit /b %ERRORLEVEL%
)

set /p COMMIT_MSG="Digite a mensagem do commit (ou Enter para padrao): "
if "%COMMIT_MSG%"=="" set COMMIT_MSG="chore: update project state and documentation"

git commit -m "%COMMIT_MSG%"

git branch -M main
git push origin main
if %ERRORLEVEL% neq 0 (
    echo.
    echo ========================================================
    echo   [ERRO] FALHA AO ENVIAR DADOS PARA O REPOSITORIO REMOTO!
    echo   Verifique sua conexao ou permissoes de acesso ao GitHub.
    echo ========================================================
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ========================================================
echo   SINCRONIZACAO CONCLUIDA COM SUCESSO!
echo ========================================================
pause
