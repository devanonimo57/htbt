@echo off
chcp 65001 >nul
echo ========================================================
echo   ENVIANDO NOVA VERSAO 1:1 SHARKBOT PARA O GITHUB/RENDER
echo ========================================================
cd /d "C:\Users\Yago\.gemini\antigravity\scratch\hotbot-ai"
git add -A
git commit -m "Complete 1:1 Sharkbot SaaS clone with all 13 screens and full backend"
git branch -M main
git push -u origin main --force
echo.
echo ========================================================
echo   CONCLUIDO! O Render vai atualizar em 1-2 minutos:
echo   https://hotbot-saas.onrender.com/dashboard/
echo ========================================================
pause
