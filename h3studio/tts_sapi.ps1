param(
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$Text,
    [string]$Voice = "Microsoft Huihui Desktop",
    [ValidateRange(-10,10)][int]$Rate = 0,
    [ValidateRange(0,100)][int]$Volume = 100
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speaker.SelectVoice($Voice)
    $speaker.Rate = $Rate
    $speaker.Volume = $Volume
    $speaker.SetOutputToWaveFile($Output)
    $speaker.Speak($Text)
} finally {
    $speaker.Dispose()
}
