@echo off
chcp 65001 >nul
echo ========================================================
echo   SINCRONIZANDO REPOSITORIO COM O GITHUB (BRANCH MAIN)
echo ========================================================
cd /d "%~dp0"
git add -A
set /p COMMIT_MSG="Digite a mensagem do commit (ou Enter para padrao): "
if "%COMMIT_MSG%"=="" set COMMIT_MSG="chore: update project state and documentation"
git commit -m "%COMMIT_MSG%"
git branch -M main
git push origin main
echo.
echo ========================================================
echo   SINCRONIZACAO CONCLUIDA COM SUCESSO!
echo ========================================================
pause
