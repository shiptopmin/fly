@echo off
REM Anaconda Prompt 에서는 그냥  python main.py  /  python analyze.py  로 실행하면 됩니다.
REM 이 파일은 일반 명령 프롬프트/더블클릭용으로, Miniconda 의 python 을 직접 호출합니다.
REM 사용 예)  run.bat            -> 수집 (headless)
REM          run.bat --debug    -> 수집 (브라우저 표시)
"%USERPROFILE%\miniconda3\python.exe" main.py %*
