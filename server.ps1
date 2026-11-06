# Sports Dashboard local helper.
# Serves the dashboard at http://localhost:8765 and relays ESPN + Open-Meteo requests
# so the browser never has to call those sites directly. Close this window to stop it.
$port = 8765
$root = $PSScriptRoot
Add-Type -AssemblyName System.Net.Http
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$http = New-Object System.Net.Http.HttpClient
$http.Timeout = [TimeSpan]::FromSeconds(15)
$http.DefaultRequestHeaders.UserAgent.ParseAdd('Mozilla/5.0 (Windows NT 10.0; Win64; x64) SportsDashboard/1.0')

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$port/")
try { $listener.Start() } catch {
  Write-Host "Dashboard is already running. Opening it..."
  Start-Process "http://localhost:$port/"
  Start-Sleep 2
  exit
}
Write-Host ""
Write-Host "  Sports Dashboard is running at http://localhost:$port"
Write-Host "  Leave this window open while you use it. Close it to stop."
Write-Host ""
Start-Process "http://localhost:$port/"

$types = @{ '.html' = 'text/html; charset=utf-8'; '.js' = 'application/javascript; charset=utf-8'; '.css' = 'text/css'; '.json' = 'application/json' }

while ($listener.IsListening) {
  try { $ctx = $listener.GetContext() } catch { break }
  $req = $ctx.Request; $res = $ctx.Response; $bytes = $null
  try {
    $path = $req.Url.AbsolutePath; $q = $req.Url.Query; $target = $null
    if ($path.StartsWith('/api/espn/')) { $target = 'https://site.api.espn.com/apis/site/v2/sports/' + $path.Substring(10) + $q }
    elseif ($path.StartsWith('/api/espnv2/')) { $target = 'https://site.api.espn.com/apis/v2/sports/' + $path.Substring(12) + $q }
    elseif ($path.StartsWith('/api/espnweb/')) { $target = 'https://site.web.api.espn.com/apis/' + $path.Substring(13) + $q }
    elseif ($path.StartsWith('/data/') -and $path.EndsWith('.json')) {
      # Latest picks/history from the live site; fall back to the local copy if offline
      try {
        $r = $http.GetAsync('https://kfarris85.github.io/dashboard' + $path + '?t=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()).Result
        if ($r.IsSuccessStatusCode) { $bytes = $r.Content.ReadAsByteArrayAsync().Result; $res.ContentType = 'application/json; charset=utf-8'; $res.Headers.Add('Cache-Control','no-store'); $res.ContentLength64 = $bytes.Length; $res.OutputStream.Write($bytes,0,$bytes.Length); $res.Close(); continue }
      } catch {}
    }
    if ($false) {}
    elseif ($path -eq '/api/geo') { $target = 'https://geocoding-api.open-meteo.com/v1/search' + $q }
    elseif ($path -eq '/api/wx') { $target = 'https://api.open-meteo.com/v1/forecast' + $q }

    if ($target) {
      $r = $http.GetAsync($target).Result
      $bytes = $r.Content.ReadAsByteArrayAsync().Result
      $res.StatusCode = [int]$r.StatusCode
      $res.ContentType = 'application/json; charset=utf-8'
      Write-Host ("{0}  {1}  {2}" -f (Get-Date -Format 'h:mm:ss tt'), [int]$r.StatusCode, $target.Split('?')[0])
    } else {
      $file = if ($path -eq '/') { 'Sports Dashboard.html' } else { [Uri]::UnescapeDataString($path.TrimStart('/')) }
      $full = [IO.Path]::GetFullPath((Join-Path $root $file))
      if ($full.StartsWith($root) -and (Test-Path -LiteralPath $full -PathType Leaf)) {
        $bytes = [IO.File]::ReadAllBytes($full)
        $ct = $types[[IO.Path]::GetExtension($full).ToLower()]
        if ($ct) { $res.ContentType = $ct }
      } else {
        $res.StatusCode = 404; $bytes = [Text.Encoding]::UTF8.GetBytes('Not found')
      }
    }
    $res.Headers.Add('Cache-Control', 'no-store')
  } catch {
    $msg = $_.Exception.Message
    if ($_.Exception.InnerException) { $msg = $_.Exception.InnerException.Message }
    Write-Host "ERROR: $msg" -ForegroundColor Red
    $res.StatusCode = 502
    $res.ContentType = 'application/json'
    $bytes = [Text.Encoding]::UTF8.GetBytes('{"error":"' + ($msg -replace '["\\]', '') + '"}')
  }
  try { $res.ContentLength64 = $bytes.Length; $res.OutputStream.Write($bytes, 0, $bytes.Length) } catch {}
  try { $res.Close() } catch {}
}
