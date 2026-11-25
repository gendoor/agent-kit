@echo off
rem Изолированные git worktree для параллельных субагентов (Windows).
rem Логика в agent_worktree.py; парный файл для Linux - agent-worktree.
rem   .\scripts\agent-worktree.cmd create|list|diff|merge|remove ...   справка: --help
setlocal
set "PYTHONUTF8=1"
where py >nul 2>&1
if errorlevel 1 goto :try_python
py -3 "%~dp0agent_worktree.py" %*
exit /b

:try_python
python "%~dp0agent_worktree.py" %*
exit /b
