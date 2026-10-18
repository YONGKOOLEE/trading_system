#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
  M2 PAPER 무인 운영 예약 작업 등록 (CP-037 T-055, D-224·D-229·D-231, 승인 A-113 Q-1 (가)). 사용자가 관리자 PowerShell에서
  한 번 실행한다. **자동 실행 등록 승인(D-231, T-056 전 별도) 뒤에만** 실행한다.

.DESCRIPTION
  M2 체크아웃(C:\AI\trading_system\m2-binance)의 감독 프로세스를 현재 로그인 사용자 계정으로 등록한다
  (작업 이름 접두어 TradingSystem-PAPER-M2-, M1 작업 TradingSystem-PAPER-ClockSync·TradingSystem-PAPER-Autopilot과 겹치지 않음).
    1) TradingSystem-PAPER-M2-Supervisor : 트리거 ① 사용자 로그온 시 ② 매일 1회 보조(-DailyAt, 기본 07:30)
         pythonw.exe scripts\m2-supervisor.py --mode PAPER --market M2  (일반 권한, 창 없음, 실행 시간 상한 없음)
         감독이 이미 돌고 있으면 작업 스케줄러가 새 실행을 무시하고(IgnoreNew), 그래도 시작되면 감독 잠금으로 즉시 끝난다.
    2) TradingSystem-PAPER-M2-ClockSync  : 매일 08:52(KST = UTC 23:52, M2 주문 창 밖)  w32tm /resync  (최고 권한, 5분 상한)
         -SkipClockSync로 빼고 등록할 수 있다.
  - M2 PAPER(바이낸스 Spot 테스트넷) 전용. LIVE·BACKTEST·M1 작업은 만들지 않는다. 감독은 --mode PAPER --market M2 외에는 거부한다.
  - 비밀값(키·토큰·계좌번호)을 명령줄·작업 정의에 넣지 않는다. 감독이 실행 때 M2 체크아웃의 .env.paper를 읽는다.
  - 로그온한 동안만 실행(Interactive) — 암호를 저장하지 않는다. 재부팅 뒤 자동 로그인(A-098)이 되어야 다시 시작된다.
  - 같은 이름의 작업이 이미 있으면 아무것도 바꾸지 않고 멈춘다(먼저 scripts\schedule-unregister-m2.ps1).
  - [미확인] 실행 시간 상한 없음 = ExecutionTimeLimit 0(PT0S)로 지정한다. "0이면 상한 없음"과, 작업을 끝낼 때(작업 스케줄러
    "끝내기") 감독이 띄운 run 자식 프로세스도 함께 끝나는지는 공식 문서로 확인하지 못했다(네트워크 없이 작성). 등록 뒤
    Get-ScheduledTask로 설정 값을 확인하고(설명서 §13), 감독을 끝낼 때는 작업 끝내기 대신 m2-supervisor.py --stop을 쓴다.
  - 에이전트는 이 스크립트를 실행하지 않는다. 등록·해제는 사용자가 한다 (CLAUDE.md §4 자동 실행 등록, D-231).

.EXAMPLE
  관리자 권한 PowerShell에서:
    cd C:\AI\trading_system\m2-binance
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\schedule-register-m2.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$Root = '',
    [string]$VenvRoot = 'C:\AI\trading_system\trading_system',   # .venv가 있는 저장소 (run-m2.py 설명과 같은 실행 파일)
    [string]$DailyAt = '07:30',                                  # 매일 보조 트리거 (PC 시각)
    [switch]$SkipClockSync,
    [switch]$DryRun     # 경로·모드 줄·계정·시간대만 확인하고 등록하지 않는다
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1에서 -File로 실행하면 param() 기본값 계산 시점에 $PSScriptRoot가 비어 있다(M1 스크립트와 같은 처리).
if (-not $Root) {
    $here = $PSScriptRoot
    if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Root = Split-Path -Parent $here
}
$Prefix = 'TradingSystem-PAPER-M2-'
$SupTask = $Prefix + 'Supervisor'
$ClockTask = $Prefix + 'ClockSync'

$Root = (Resolve-Path -LiteralPath $Root).Path
if ((Split-Path -Leaf $Root) -ne 'm2-binance') {
    throw "M2 체크아웃이 아님: $Root — C:\AI\trading_system\m2-binance에서 실행 (M1 저장소에 M2 작업을 등록하지 않는다)"
}
$M2App = Join-Path $Root 'config\m2\app.paper.toml'
$Pythonw = Join-Path $VenvRoot '.venv\Scripts\pythonw.exe'
$Script = Join-Path $Root 'scripts\m2-supervisor.py'
$W32tm = Join-Path $env:SystemRoot 'System32\w32tm.exe'
foreach ($path in @($M2App, $Pythonw, $Script, $W32tm)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "파일 없음: $path"
    }
}
# 예약 실행에는 환경 변수가 없어 감독이 M2 체크아웃 루트 .env의 TRADE_MODE 한 줄로 모드를 확인한다(m2_supervisor
# mode_market_refusal). 값은 출력하지 않고 TRADE_MODE 줄만 본다.
$ModeFile = Join-Path $Root '.env'
$modeLine = $null
if (Test-Path -LiteralPath $ModeFile) {
    $modeLine = Get-Content -LiteralPath $ModeFile | Where-Object { $_ -match '^\s*TRADE_MODE\s*=' } | Select-Object -First 1
}
if (-not $modeLine -or ($modeLine -replace '\s', '') -ne 'TRADE_MODE=PAPER') {
    throw "M2 체크아웃 루트 .env에 'TRADE_MODE=PAPER' 한 줄이 없습니다 ($ModeFile). 감독은 이 줄로 모드를 확인하므로 없으면 시작을 거부합니다. 설명서 §13.2를 먼저 하세요."
}
# 시계 동기화 08:52는 KST(UTC+9) 기준 시각이다(= UTC 23:52, 주문 창 밖). PC 시간대가 UTC+9가 아니면 멈춘다.
if ((Get-TimeZone).BaseUtcOffset -ne [TimeSpan]::FromHours(9)) {
    throw ("PC 시간대가 UTC+9가 아님: {0} — 시계 동기화 시각(08:52 = UTC 23:52)이 어긋나므로 등록하지 않습니다" -f (Get-TimeZone).Id)
}
$names = @($SupTask)
if (-not $SkipClockSync) { $names += $ClockTask }
foreach ($name in $names) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        throw "이미 등록된 작업: $name — 아무것도 바꾸지 않음. 바꾸려면 먼저 scripts\schedule-unregister-m2.ps1 실행"
    }
}
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($DryRun) {
    Write-Host '확인만 (등록 안 함)'
    Write-Host ('  M2 체크아웃: {0}' -f $Root)
    Write-Host ('  실행 파일: {0}' -f $Pythonw)
    Write-Host ('  감독: {0} --mode PAPER --market M2' -f $Script)
    Write-Host '  모드 줄: TRADE_MODE=PAPER 확인'
    Write-Host ('  시간대: {0} (UTC+9 확인)' -f (Get-TimeZone).Id)
    Write-Host ('  등록될 작업: {0}' -f ($names -join ', '))
    Write-Host ('  감독 트리거: 로그온 시 + 매일 {0}, 실행 시간 상한 없음(0)' -f $DailyAt)
    Write-Host ('  등록될 계정: {0}  (평소 로그인 계정과 같아야 함)' -f $User)
    exit 0
}

# 1) 감독 (일반 권한, 창 없음, 상한 없음). 로그온 시 시작해 계속 돈다 — 시작 점검 → run → K8 자동 재개 → 감시·제한 재시작 → UTC 일과.
$supArgs = '"{0}" --mode PAPER --market M2' -f $Script
$supAction = New-ScheduledTaskAction -Execute $Pythonw -Argument $supArgs -WorkingDirectory $Root
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$supPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$supSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $SupTask -Action $supAction -Trigger @($logonTrigger, $dailyTrigger) `
    -Principal $supPrincipal -Settings $supSettings `
    -Description 'M2 PAPER 감독 프로세스 (CP-037, 바이낸스 테스트넷 PAPER 전용). 정지: m2-supervisor.py --stop. 해제: scripts\schedule-unregister-m2.ps1' | Out-Null

# 2) 시계 동기화 (관리자 권한 필요 — 이 작업만 최고 권한). 매일 08:52 KST = UTC 23:52 (M2 주문 창 UTC 00:10~23:50 밖).
if (-not $SkipClockSync) {
    $clockAction = New-ScheduledTaskAction -Execute $W32tm -Argument '/resync'
    $clockTrigger = New-ScheduledTaskTrigger -Daily -At '08:52'
    $clockPrincipal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
    $clockSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $ClockTask -Action $clockAction -Trigger $clockTrigger -Principal $clockPrincipal `
        -Settings $clockSettings -Description 'M2 PAPER 시계 동기화 (CP-037 D-229). 해제: scripts\schedule-unregister-m2.ps1' | Out-Null
}

Write-Host '등록 완료 (사용자:' $User ')'
Get-ScheduledTask -TaskName ($Prefix + '*') | Format-Table TaskName, State -AutoSize
foreach ($name in $names) {
    $task = Get-ScheduledTask -TaskName $name
    $info = Get-ScheduledTaskInfo -TaskName $name
    Write-Host ('  {0}  다음 실행 {1}  실행 시간 상한 {2}' -f $name, $info.NextRunTime, $task.Settings.ExecutionTimeLimit)
}
