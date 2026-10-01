@echo off
chcp 65001 >nul
rem 부캐 육성: 스토리를 넘기며 98레벨까지 키운다(다른 레벨은 "스토리.bat 99"). 준비는 던전.bat과 같다.
call "%~dp0던전.bat" --story %*
