<#
  Flipkart + Amazon Price History Tracker (Windows PowerShell 5.1, no installs needed)

  Usage:
    .\PriceTracker.ps1                 Start the web UI at http://localhost:8765 and open it
    .\PriceTracker.ps1 -Add <url> [-Target 49999]   Track a product from the command line
    .\PriceTracker.ps1 -Check          Check every tracked product once (used by the scheduler)
    .\PriceTracker.ps1 -List           Print tracked products and their price stats
    .\PriceTracker.ps1 -Schedule       Register a daily Windows scheduled task that runs -Check
    .\PriceTracker.ps1 -Unschedule     Remove that scheduled task
#>
[CmdletBinding()]
param(
    [string]$Add,
    [double]$Target = 0,
    [switch]$Check,
    [switch]$List,
    [switch]$Schedule,
    [string]$ScheduleTime = '09:00',
    [switch]$Unschedule,
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root     = $PSScriptRoot
$DataDir  = Join-Path $Root 'data'
$DataFile = Join-Path $DataDir 'products.json'
$LogFile  = Join-Path $DataDir 'check.log'
$UiFile   = Join-Path $Root 'ui.html'
$TaskName = 'PriceHistoryTracker-DailyCheck'
$UA       = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
$Rupee    = [string][char]0x20B9

if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }

# ---------------------------------------------------------------- storage

function Load-Products {
    $list = New-Object System.Collections.Generic.List[object]
    if (Test-Path $DataFile) {
        $raw = Get-Content -Raw -Encoding UTF8 $DataFile
        if ($raw -and $raw.Trim()) {
            (ConvertFrom-Json $raw) | ForEach-Object { if ($_) { $list.Add($_) } }
        }
    }
    return ,$list
}

function Save-Products($list) {
    $json = ConvertTo-Json -InputObject ([object[]]$list.ToArray()) -Depth 8
    $tmp = "$DataFile.tmp"
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -Force $tmp $DataFile
}

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

# ---------------------------------------------------------------- scraping

function Get-Site([string]$url) {
    if ($url -match '(^|[./])(amazon\.[a-z.]+|amzn\.(in|to|eu|com))(/|$)') { return 'amazon' }
    if ($url -match '(^|[./])(flipkart\.com|fkrt\.(it|cc|co))(/|$)') { return 'flipkart' }
    return $null
}

function Normalize-Url([string]$url, [string]$site) {
    $u = [uri]$url
    if ($site -eq 'amazon' -and $url -match '/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})') {
        return "https://$($u.Host)/dp/$($Matches[1])"
    }
    if ($site -eq 'flipkart' -and $u.Host -match 'flipkart\.com$' -and $u.AbsolutePath -match '/p/') {
        $q = ''
        if ($u.Query -match '[?&]pid=([A-Za-z0-9]+)') { $q = "?pid=$($Matches[1])" }
        return "https://www.flipkart.com$($u.AbsolutePath)$q"
    }
    return $url
}

function Fetch-Page([string]$url) {
    $headers = @{
        'Accept'          = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        'Accept-Language' = 'en-IN,en;q=0.9'
    }
    $r = Invoke-WebRequest -Uri $url -UserAgent $UA -Headers $headers -UseBasicParsing -TimeoutSec 30 -MaximumRedirection 10
    $final = $url
    try { if ($r.BaseResponse.ResponseUri) { $final = $r.BaseResponse.ResponseUri.AbsoluteUri } } catch {}
    [pscustomobject]@{ Html = [string]$r.Content; Url = $final }
}

function To-Number([string]$s) {
    if (-not $s) { return $null }
    $clean = ($s -replace '[^\d.]', '')
    if (-not $clean) { return $null }
    return [double]::Parse($clean, [Globalization.CultureInfo]::InvariantCulture)
}

function Decode([string]$s) {
    if ($null -eq $s) { return $null }
    return ([Net.WebUtility]::HtmlDecode($s) -replace '\s+', ' ').Trim()
}

function Parse-Amazon([string]$html) {
    if ($html -match 'validateCaptcha|Type the characters you see in this image|api-services-support@amazon\.com') {
        throw 'Amazon showed a bot-check page instead of the product. Wait a while and try again.'
    }
    $title = $null; $price = $null; $image = $null; $mrp = $null

    if ($html -match 'id="productTitle"[^>]*>\s*([^<]+)') { $title = Decode $Matches[1] }
    elseif ($html -match '<meta name="title" content="([^"]+)"') { $title = Decode $Matches[1] }

    foreach ($anchor in 'apex-pricetopay-value', 'corePriceDisplay_desktop_feature_div', 'corePrice_feature_div', 'corePrice_desktop', 'apex_desktop') {
        $i = $html.IndexOf($anchor)
        if ($i -ge 0) {
            $seg = $html.Substring($i, [Math]::Min(6000, $html.Length - $i))
            if ($seg -match 'a-price-whole">([\d,]+)') { $price = To-Number $Matches[1]; break }
        }
    }
    if (-not $price -and $html -match 'id="priceblock_(?:ourprice|dealprice|saleprice)"[^>]*>([^<]+)') { $price = To-Number $Matches[1] }
    if (-not $price -and $html -match 'a-price-whole">([\d,]+)') { $price = To-Number $Matches[1] }

    if ($html -match 'a-price a-text-price"[^>]*data-a-strike="true"[^>]*>\s*<span class="a-offscreen">([^<]+)') { $mrp = To-Number $Matches[1] }

    if ($html -match '"hiRes":"(https://[^"]+)"') { $image = $Matches[1] }
    elseif ($html -match 'data-old-hires="(https://[^"]+)"') { $image = $Matches[1] }
    elseif ($html -match 'id="landingImage"[^>]*src="(https://[^"]+)"') { $image = $Matches[1] }

    if (-not $price) {
        if ($html -match 'id="outOfStock"|Currently unavailable') { throw 'Product is currently unavailable on Amazon.' }
        throw 'Could not find a price on the Amazon page (layout may have changed).'
    }
    [pscustomobject]@{ Title = $title; Price = $price; Mrp = $mrp; Image = $image }
}

function Parse-Flipkart([string]$html) {
    $title = $null; $price = $null; $image = $null; $mrp = $null

    $scripts = [regex]::Matches($html, '(?s)<script[^>]*application/ld\+json[^>]*>(.*?)</script>')
    foreach ($s in $scripts) {
        try { $data = $s.Groups[1].Value | ConvertFrom-Json } catch { continue }
        foreach ($item in @($data | ForEach-Object { $_ })) {
            if ($item.'@type' -eq 'Product') {
                if (-not $title) { $title = Decode $item.name }
                if (-not $image -and $item.image) { $image = @($item.image)[0] }
                $offer = @($item.offers)[0]
                if ($offer -and $offer.price) { $price = [double]$offer.price }
                elseif ($offer -and $offer.lowPrice) { $price = [double]$offer.lowPrice }
            }
        }
        if ($price) { break }
    }
    if (-not $title -and $html -match '<meta property="og:title" content="([^"]+)"') { $title = Decode $Matches[1] }
    if (-not $image -and $html -match '<meta property="og:image" content="([^"]+)"') { $image = $Matches[1] }
    if (-not $price) {
        $m = [regex]::Match($html, ">$Rupee([\d,]+)<")
        if ($m.Success) { $price = To-Number $m.Groups[1].Value }
    }
    if (-not $price) {
        if ($html -match 'Sold Out|Currently Unavailable|Coming Soon') { throw 'Product is sold out / unavailable on Flipkart.' }
        throw 'Could not find a price on the Flipkart page (it may have blocked the request; try again later).'
    }
    [pscustomobject]@{ Title = $title; Price = $price; Mrp = $mrp; Image = $image }
}

function Get-ProductInfo([string]$url) {
    $site = Get-Site $url
    if (-not $site) { throw 'Only Amazon and Flipkart product links are supported.' }
    $page = Fetch-Page $url
    $finalSite = Get-Site $page.Url
    if ($finalSite) { $site = $finalSite }
    $info = if ($site -eq 'amazon') { Parse-Amazon $page.Html } else { Parse-Flipkart $page.Html }
    $info | Add-Member -NotePropertyName Site -NotePropertyValue $site
    $info | Add-Member -NotePropertyName Url  -NotePropertyValue (Normalize-Url $page.Url $site)
    return $info
}

# ---------------------------------------------------------------- tracking

function Now-Iso { (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss') }

function Add-Product([string]$url, $target) {
    $url = $url.Trim()
    if ($url -notmatch '^https?://') { $url = "https://$url" }
    $list = Load-Products
    $info = Get-ProductInfo $url
    $existing = $list | Where-Object { $_.url -eq $info.Url } | Select-Object -First 1
    if ($existing) { throw "Already tracking: $($existing.title)" }
    $now = Now-Iso
    $p = [pscustomobject]@{
        id          = ([guid]::NewGuid().ToString('N').Substring(0, 10))
        url         = $info.Url
        site        = $info.Site
        title       = $(if ($info.Title) { $info.Title } else { $info.Url })
        image       = $info.Image
        mrp         = $info.Mrp
        target      = $(if ($target -and [double]$target -gt 0) { [double]$target } else { $null })
        added       = $now
        lastChecked = $now
        lastError   = $null
        history     = @([pscustomobject]@{ t = $now; p = $info.Price })
    }
    $list.Add($p)
    Save-Products $list
    return $p
}

function Update-Product($p) {
    $now = Now-Iso
    try {
        $info = Get-ProductInfo $p.url
        if ($info.Title) { $p.title = $info.Title }
        if ($info.Image) { $p.image = $info.Image }
        if ($null -ne $info.Mrp) { $p.mrp = $info.Mrp }
        $p.history = @($p.history) + @([pscustomobject]@{ t = $now; p = $info.Price })
        $p.lastError = $null
    } catch {
        $p.lastError = $_.Exception.Message
    }
    $p.lastChecked = $now
    return $p
}

function Check-Products([string]$id) {
    $list = Load-Products
    $targets = @($list | Where-Object { -not $id -or $_.id -eq $id })
    $i = 0
    foreach ($p in $targets) {
        if ($i++ -gt 0) { Start-Sleep -Milliseconds (Get-Random -Minimum 1500 -Maximum 3500) }  # be polite
        [void](Update-Product $p)
    }
    Save-Products $list
    return ,$targets
}

function Get-Stats($p) {
    $prices = @($p.history | ForEach-Object { [double]$_.p })
    if (-not $prices.Count) { return $null }
    $m = $prices | Measure-Object -Minimum -Maximum -Average
    [pscustomobject]@{ Current = $prices[-1]; Low = $m.Minimum; High = $m.Maximum; Avg = [Math]::Round($m.Average, 0); Count = $prices.Count }
}

function Show-Alert([string]$title, [string]$text) {
    try {
        Add-Type -AssemblyName System.Windows.Forms, System.Drawing
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon = [System.Drawing.SystemIcons]::Information
        $n.Visible = $true
        $n.ShowBalloonTip(10000, $title, $text, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Seconds 8
        $n.Dispose()
    } catch {}
}

# ---------------------------------------------------------------- web server

function Send-Response($ctx, [int]$code, [string]$body, [string]$type = 'application/json; charset=utf-8') {
    $bytes = [Text.Encoding]::UTF8.GetBytes($body)
    $ctx.Response.StatusCode = $code
    $ctx.Response.ContentType = $type
    $ctx.Response.Headers['Cache-Control'] = 'no-store'
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.OutputStream.Close()
}

function Send-Json($ctx, $obj, [int]$code = 200) {
    Send-Response $ctx $code (ConvertTo-Json -InputObject $obj -Depth 8 -Compress)
}

function Read-Body($ctx) {
    $reader = New-Object IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
    $raw = $reader.ReadToEnd(); $reader.Close()
    if ($raw) { return ($raw | ConvertFrom-Json) }
    return [pscustomobject]@{}
}

function Handle-Request($ctx) {
    $path = $ctx.Request.Url.AbsolutePath
    $method = $ctx.Request.HttpMethod
    try {
        switch -Regex ("$method $path") {
            '^GET /$' {
                Send-Response $ctx 200 ([IO.File]::ReadAllText($UiFile, [Text.Encoding]::UTF8)) 'text/html; charset=utf-8'; return
            }
            '^GET /api/products$' {
                Send-Json $ctx ([object[]](Load-Products).ToArray()); return
            }
            '^POST /api/add$' {
                $b = Read-Body $ctx
                $p = Add-Product ([string]$b.url) $b.target
                Send-Json $ctx $p; return
            }
            '^POST /api/check$' {
                $b = Read-Body $ctx
                $done = Check-Products ([string]$b.id)
                Send-Json $ctx @{ checked = @($done).Count; errors = @($done | Where-Object { $_.lastError }).Count }; return
            }
            '^POST /api/target$' {
                $b = Read-Body $ctx
                $list = Load-Products
                $p = $list | Where-Object { $_.id -eq $b.id } | Select-Object -First 1
                if (-not $p) { throw 'Product not found' }
                $p.target = $(if ($b.target -and [double]$b.target -gt 0) { [double]$b.target } else { $null })
                Save-Products $list
                Send-Json $ctx $p; return
            }
            '^POST /api/delete$' {
                $b = Read-Body $ctx
                $list = Load-Products
                $keep = New-Object System.Collections.Generic.List[object]
                $list | Where-Object { $_.id -ne $b.id } | ForEach-Object { $keep.Add($_) }
                Save-Products $keep
                Send-Json $ctx @{ ok = $true }; return
            }
            '^GET /api/export\.csv$' {
                $sb = New-Object Text.StringBuilder
                [void]$sb.AppendLine('product,site,url,timestamp,price')
                foreach ($p in (Load-Products)) {
                    foreach ($h in @($p.history)) {
                        [void]$sb.AppendLine(('"{0}",{1},{2},{3},{4}' -f ($p.title -replace '"', '""'), $p.site, $p.url, $h.t, $h.p))
                    }
                }
                $ctx.Response.Headers['Content-Disposition'] = 'attachment; filename="price-history.csv"'
                Send-Response $ctx 200 $sb.ToString() 'text/csv; charset=utf-8'; return
            }
            default { Send-Json $ctx @{ error = 'Not found' } 404 }
        }
    } catch {
        Write-Host "  ! $($_.Exception.Message)" -ForegroundColor Yellow
        try { Send-Json $ctx @{ error = $_.Exception.Message } 400 } catch {}
    }
}

function Start-Server {
    # Try the requested port, then the next few, in case another app already uses it.
    $listener = $null; $prefix = $null; $lastErr = $null
    foreach ($p in $Port..($Port + 20)) {
        $candidate = New-Object Net.HttpListener
        $candidate.Prefixes.Add("http://localhost:$p/")
        try { $candidate.Start(); $listener = $candidate; $prefix = "http://localhost:$p/"; break }
        catch { $lastErr = $_.Exception.Message; $candidate.Close() }
    }
    if (-not $listener) { throw "Could not start the web server on ports $Port-$($Port + 20): $lastErr" }
    Write-Host ""
    Write-Host "  Price History Tracker running at $prefix" -ForegroundColor Green
    Write-Host "  Data file: $DataFile"
    Write-Host "  Press Ctrl+C to stop."
    Write-Host ""
    if (-not $NoBrowser) { Start-Process $prefix }
    try {
        while ($listener.IsListening) {
            $async = $listener.BeginGetContext($null, $null)
            while (-not $async.AsyncWaitHandle.WaitOne(300)) { }
            $ctx = $listener.EndGetContext($async)
            Write-Host ("  {0} {1} {2}" -f (Get-Date -Format 'HH:mm:ss'), $ctx.Request.HttpMethod, $ctx.Request.Url.AbsolutePath)
            Handle-Request $ctx
        }
    } finally {
        $listener.Stop(); $listener.Close()
    }
}

# ---------------------------------------------------------------- entry point

if ($Add) {
    $p = Add-Product $Add $Target
    Write-Host ("Tracking [{0}] {1}  -  {2}{3:N0}" -f $p.site, $p.title, $Rupee, $p.history[0].p) -ForegroundColor Green
}
elseif ($Check) {
    Write-Log 'Checking all products...'
    $checked = Check-Products ''
    $alerts = @()
    foreach ($p in $checked) {
        $s = Get-Stats $p
        if ($p.lastError) { Write-Log ("  ERROR  {0}: {1}" -f $p.title, $p.lastError); continue }
        Write-Log ("  {0}{1:N0}  (low {0}{2:N0})  {3}" -f $Rupee, $s.Current, $s.Low, $p.title)
        if ($p.target -and $s.Current -le [double]$p.target) {
            $alerts += ("{0}: {1}{2:N0} (target {1}{3:N0})" -f $p.title.Substring(0, [Math]::Min(50, $p.title.Length)), $Rupee, $s.Current, [double]$p.target)
        }
    }
    if ($alerts.Count) { Show-Alert 'Price drop! Target reached' ($alerts -join "`n") }
}
elseif ($List) {
    foreach ($p in (Load-Products)) {
        $s = Get-Stats $p
        Write-Host ("[{0}] {1}" -f $p.site, $p.title) -ForegroundColor Cyan
        Write-Host ("    now {0}{1:N0} | low {0}{2:N0} | high {0}{3:N0} | avg {0}{4:N0} | {5} checks | target {6}" -f $Rupee, $s.Current, $s.Low, $s.High, $s.Avg, $s.Count, $(if ($p.target) { "$Rupee{0:N0}" -f [double]$p.target } else { '-' }))
    }
}
elseif ($Schedule) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"{0}`" -Check" -f $PSCommandPath)
    $trigger = New-ScheduledTaskTrigger -Daily -At $ScheduleTime
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description 'Checks tracked Amazon/Flipkart prices daily' -Force | Out-Null
    Write-Host "Scheduled daily price check at $ScheduleTime (task '$TaskName')." -ForegroundColor Green
}
elseif ($Unschedule) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'."
}
else {
    Start-Server
}
