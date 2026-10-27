# Switches a Tactrix OpenPort 2.0 (USB 0403:CC4D) between Microsoft's WinUSB
# driver, which NC Flash's built-in OpenPort driver uses, and Tactrix's own
# driver. NC Flash runs it elevated (one UAC prompt) instead of Zadig. NC Flash
# passes the script text itself (-EncodedCommand), never a path to it.
#
# -Action Install : bind the cable to WinUSB. Writes a WinUSB setup file that
#   reuses Windows' own winusb.inf (Windows 8 and later need nothing else),
#   signs its catalog with a one-time certificate made here (non-exportable key,
#   deleted right after signing and before the certificate is trusted), trusts
#   the certificate, stages the package and forces it onto the cable.
# -Action Restore : remove every NC Flash or Zadig WinUSB setup for the cable
#   and NC Flash's certificates, so Windows goes back to Tactrix's driver.
#
# Logs to %ProgramData%\NC Flash\OpenPort\<LogName> (admin-only writes).
# Exit codes: 0 done, 1 failed, 3 done but Windows needs a restart, 5 another
# switch is running. With no -Action it only defines its functions (tests).

param(
    [ValidateSet('Install', 'Restore', '')][string]$Action = '',
    [ValidatePattern('^[A-Za-z0-9_-]*\.log$|^$')][string]$LogName = ''
)

$HardwareId = 'USB\VID_0403&PID_CC4D'
$InterfaceGuid = '{41ac5a38-49af-4a2a-bb46-271995d78bd9}'
$CertSubject = 'CN=NC Flash OpenPort WinUSB setup (this PC only)'
$InfName = 'ncflash_openport.inf'
$PowerShellModules = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'

function New-OpenPortInf([string]$Marker, [datetime]$Date) {
    # DriverVer must be MM/dd/yyyy whatever the PC's region.
    $driverDate = $Date.ToString('MM\/dd\/yyyy', [Globalization.CultureInfo]::InvariantCulture)
    $lines = @(
        "; NC Flash: binds the Tactrix OpenPort 2.0 to Windows' own WinUSB driver.",
        "; NC Flash package $Marker",
        '[Version]',
        'Signature   = "$Windows NT$"',
        'Class       = USBDevice',
        'ClassGuid   = {88bae032-5a81-49f0-bc3d-a4ff138216d6}',
        'Provider    = %Provider%',
        'CatalogFile = ncflash_openport.cat',
        "DriverVer   = $driverDate,1.0.0.0",
        '',
        '[Manufacturer]',
        '%Provider% = OpenPort,NTamd64,NTx86,NTarm64',
        ''
    )
    foreach ($arch in 'NTamd64', 'NTx86', 'NTarm64') {
        $lines += "[OpenPort.$arch]", "%DeviceName% = USB_Install, $HardwareId", ''
    }
    $lines += @(
        '[USB_Install]', 'Include = winusb.inf', 'Needs   = WINUSB.NT', '',
        '[USB_Install.Services]', 'Include = winusb.inf', 'Needs   = WINUSB.NT.Services', '',
        '[USB_Install.HW]', 'AddReg = Dev_AddReg', '',
        '[Dev_AddReg]', "HKR,,DeviceInterfaceGUIDs,0x10000,`"$InterfaceGuid`"", '',
        '[Strings]', 'Provider   = "NC Flash"', 'DeviceName = "Tactrix OpenPort 2.0 (NC Flash, WinUSB)"'
    )
    return ($lines -join "`r`n") + "`r`n"
}

function Test-OurPackage([string]$Text) {
    # A WinUSB setup for the cable made by NC Flash or by Zadig (libwdi).
    return $Text -match 'VID_0403&PID_CC4D' -and $Text -match '(?i)winusb\.inf' -and
        ($Text -match '(?im)^\s*Provider\s*=\s*"?libwdi' -or $Text -match '(?im)^\s*Provider\s*=\s*"NC Flash"')
}

function New-AdminOnlyDirectory([string]$Path) {
    # Administrators and SYSTEM may write; Users may only read (the log). A folder
    # that already exists must be ours: a user could have created it, with their
    # own files inside, before NC Flash ever ran.
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "unsafe path: $Path"
        }
        $owner = (Get-Acl -LiteralPath $Path).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -ne 'S-1-5-32-544' -and $owner -ne 'S-1-5-18') {
            throw "$Path was not created by NC Flash (owner $owner); delete it and try again"
        }
    } else {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
    & "$env:SystemRoot\System32\icacls.exe" $Path /setowner '*S-1-5-32-544' /Q | Out-Null
    & "$env:SystemRoot\System32\icacls.exe" $Path /inheritance:r /grant:r `
        '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not secure $Path" }
}

function Write-Log([string]$Message) {
    if ($script:LogPath) { Add-Content -LiteralPath $script:LogPath -Value $Message -Encoding UTF8 }
}

function Get-CableState {
    # 'WinUSB (problem 0)', 'openport (problem 39)', ... or 'not plugged in'.
    try {
        $dev = Get-PnpDevice -PresentOnly -ErrorAction Stop |
            Where-Object { $_.InstanceId -like "$HardwareId\*" } | Select-Object -First 1
        if (-not $dev) { return 'not plugged in' }
        $svc = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName DEVPKEY_Device_Service).Data
        $problem = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName DEVPKEY_Device_ProblemCode).Data
        return "$svc (problem $problem)"
    } catch {
        return "unknown ($($_.Exception.Message))"
    }
}

function Invoke-PnpUtil([string[]]$Arguments) {
    # Native stderr must not become a terminating error here (Windows PowerShell 5.1).
    $ErrorActionPreference = 'Continue'
    $out = & "$env:SystemRoot\System32\pnputil.exe" @Arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-Log ("pnputil " + ($Arguments -join ' ') + " -> exit $code`r`n" + $out.Trim())
    return $code
}

function Get-OemPackages {
    Get-ChildItem -LiteralPath "$env:SystemRoot\INF" -Filter 'oem*.inf' | ForEach-Object {
        $text = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
        if ($text -and (Test-OurPackage $text)) { [pscustomobject]@{ Name = $_.Name; Text = $text } }
    }
}

function Remove-SetupCertificates([string]$KeepThumbprint = '') {
    foreach ($store in 'My', 'Root', 'TrustedPublisher') {
        Get-ChildItem "Cert:\LocalMachine\$store" |
            Where-Object { $_.Subject -eq $CertSubject -and $_.Thumbprint -ne $KeepThumbprint } |
            ForEach-Object {
                if ($store -eq 'My') { Remove-Item -LiteralPath $_.PSPath -DeleteKey }
                else { Remove-Item -LiteralPath $_.PSPath }
                Write-Log "Removed an NC Flash certificate from $store"
            }
    }
}

function Install-WinUsb([string]$RunDir) {
    $marker = [guid]::NewGuid().ToString()
    $pkg = Join-Path $RunDir 'package'
    New-Item -ItemType Directory -Path $pkg | Out-Null
    $inf = Join-Path $pkg $InfName
    [IO.File]::WriteAllText($inf, (New-OpenPortInf $marker (Get-Date)), [Text.Encoding]::ASCII)
    $catTemp = Join-Path $RunDir 'ncflash_openport.cat'
    New-FileCatalog -Path $pkg -CatalogFilePath $catTemp -CatalogVersion 2.0 | Out-Null
    $cat = Join-Path $pkg 'ncflash_openport.cat'
    Move-Item -LiteralPath $catTemp -Destination $cat

    # Sign with a one-time, non-exportable key; delete the key BEFORE the
    # certificate is trusted, so a trusted key never exists.
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $CertSubject `
        -CertStoreLocation 'Cert:\LocalMachine\My' -KeyAlgorithm RSA -KeyLength 2048 `
        -Provider 'Microsoft Software Key Storage Provider' `
        -KeyExportPolicy NonExportable -HashAlgorithm SHA256 -NotAfter (Get-Date).AddYears(30)
    $thumb = $cert.Thumbprint
    $keyName = ([Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert)).Key.UniqueName
    $cer = Join-Path $RunDir 'ncflash_openport.cer'
    try {
        Export-Certificate -Cert $cert -FilePath $cer | Out-Null
        Set-AuthenticodeSignature -FilePath $cat -Certificate $cert -HashAlgorithm SHA256 | Out-Null
    } finally {
        Remove-Item -LiteralPath "Cert:\LocalMachine\My\$thumb" -DeleteKey -ErrorAction SilentlyContinue
    }
    $keyLeft = (-not $keyName) -or [Security.Cryptography.CngKey]::Exists($keyName,
        [Security.Cryptography.CngProvider]::MicrosoftSoftwareKeyStorageProvider,
        [Security.Cryptography.CngKeyOpenOptions]::MachineKey)
    if ($keyLeft -or (Test-Path -LiteralPath "Cert:\LocalMachine\My\$thumb")) {
        throw 'the signing key could not be deleted; nothing was trusted'
    }
    Write-Log 'Signing key deleted'

    foreach ($store in 'Root', 'TrustedPublisher') {
        Import-Certificate -FilePath $cer -CertStoreLocation "Cert:\LocalMachine\$store" | Out-Null
    }
    $sig = Get-AuthenticodeSignature -FilePath $cat
    Write-Log "Catalog signature: $($sig.Status)"
    if ($sig.Status -ne 'Valid') { throw "catalog signature not valid: $($sig.StatusMessage)" }

    $code = Invoke-PnpUtil @('/add-driver', $inf)
    if ($code -ne 0 -and $code -ne 3010) { throw "pnputil could not stage the setup (exit $code)" }

    $reboot = $false
    if ((Get-CableState) -ne 'not plugged in') {
        # Force it onto the cable whatever Windows' ranking says (as Zadig does).
        Add-Type -Namespace NCFlash -Name NewDev -MemberDefinition @'
[DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern bool UpdateDriverForPlugAndPlayDevicesW(
    IntPtr hwnd, string hardwareId, string fullInfPath, uint flags, out bool reboot);
'@
        $ok = [NCFlash.NewDev]::UpdateDriverForPlugAndPlayDevicesW(
            [IntPtr]::Zero, $HardwareId, $inf, 1, [ref]$reboot)  # 1 = INSTALLFLAG_FORCE
        $err = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-Log "UpdateDriverForPlugAndPlayDevices -> $ok (error $err, restart needed $reboot)"
        if (-not $ok) { throw "Windows refused to apply the setup to the cable (error $err)" }
        Start-Sleep -Seconds 2
        $state = Get-CableState
        if (-not $reboot -and $state -notlike 'WinUSB (problem 0)') {
            throw "the cable reports '$state' after the switch"
        }
    }

    # Only now drop older NC Flash setups and certificates.
    foreach ($old in Get-OemPackages) {
        if ($old.Text -match 'Provider\s*=\s*"NC Flash"' -and $old.Text -notmatch [regex]::Escape($marker)) {
            Invoke-PnpUtil @('/delete-driver', $old.Name) | Out-Null
        }
    }
    Remove-SetupCertificates -KeepThumbprint $thumb
    return $reboot
}

function Restore-Tactrix {
    foreach ($package in Get-OemPackages) {
        Invoke-PnpUtil @('/delete-driver', $package.Name, '/uninstall', '/force') | Out-Null
    }
    Remove-SetupCertificates
    Invoke-PnpUtil @('/scan-devices') | Out-Null
    Start-Sleep -Seconds 2
    $left = @(Get-OemPackages)
    if ($left.Count) { throw "could not remove: $(($left | ForEach-Object Name) -join ', ')" }
}

if (-not $Action) { return }

$env:PSModulePath = $PowerShellModules
$ErrorActionPreference = 'Stop'
$mutex = New-Object Threading.Mutex($false, 'Global\NCFlashOpenPortDriverSetup')
if (-not $mutex.WaitOne(0)) { exit 5 }
$script:LogPath = $null
$exitCode = 1
try {
    $base = Join-Path $env:ProgramData 'NC Flash'
    New-AdminOnlyDirectory $base
    New-AdminOnlyDirectory (Join-Path $base 'OpenPort')
    if ($LogName) {
        $script:LogPath = Join-Path (Join-Path $base 'OpenPort') $LogName
        [IO.File]::Open($script:LogPath, [IO.FileMode]::CreateNew).Close()
    }
    Write-Log "NC Flash OpenPort driver setup: $Action ($(Get-Date -Format s))"
    Write-Log "Cable before: $(Get-CableState)"

    $runDir = Join-Path (Join-Path $base 'OpenPort') ("run_" + [guid]::NewGuid().ToString('N'))
    New-AdminOnlyDirectory $runDir
    $env:TEMP = $runDir; $env:TMP = $runDir  # Add-Type compiles here, not in a user folder
    try {
        if ($Action -eq 'Install') { $reboot = Install-WinUsb $runDir } else { Restore-Tactrix; $reboot = $false }
    } finally {
        Remove-Item -LiteralPath $runDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Log "Cable after: $(Get-CableState)"
    if ($reboot) { Write-Log 'RESULT: RESTART'; $exitCode = 3 } else { Write-Log 'RESULT: OK'; $exitCode = 0 }
} catch {
    Write-Log "RESULT: FAILED: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    $mutex.ReleaseMutex()
}
exit $exitCode
