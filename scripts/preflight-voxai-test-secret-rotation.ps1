& {
    # Read-only, point-in-time metadata observations only. Filesystem state may
    # change concurrently; PASS does not establish effective access, guarantee
    # physical path resolution, or prove race-free safety.
    Set-StrictMode -Version 2.0
    $ErrorActionPreference = 'Stop'
    $results = New-Object 'System.Collections.Generic.List[string]'

    $secretDirectory = 'C:\secrets\voxai-test'
    $secretFiles = @(
        @{ Label = 'postgres-secret-file'; Path = (Join-Path $secretDirectory 'postgres_password.txt') },
        @{ Label = 'redis-secret-file'; Path = (Join-Path $secretDirectory 'redis_password.txt') },
        @{ Label = 'redis-acl-file'; Path = (Join-Path $secretDirectory 'redis_acl.txt') }
    )

    $backupDirectory = $null
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $backupDirectory = Join-Path $env:LOCALAPPDATA 'VoxAI\test-secrets\rotation-backups'
    }

    function Add-Result {
        param(
            [Parameter(Mandatory = $true)][string]$Label,
            [Parameter(Mandatory = $true)][ValidateSet('PASS', 'FAIL', 'NOT VERIFIED')][string]$Status,
            [Parameter(Mandatory = $true)][string]$Reason
        )
        [void]$results.Add("$Label`: $Status ($Reason)")
    }

    function Get-PathMetadata {
        param([Parameter(Mandatory = $true)][string]$Path)

        try {
            $attributes = [IO.File]::GetAttributes($Path)
            return [pscustomobject]@{ State = 'Present'; Attributes = $attributes }
        }
        catch [IO.FileNotFoundException] {
            return [pscustomobject]@{ State = 'Missing'; Attributes = [IO.FileAttributes]::Normal }
        }
        catch [IO.DirectoryNotFoundException] {
            return [pscustomobject]@{ State = 'Unknown'; Attributes = [IO.FileAttributes]::Normal }
        }
        catch [UnauthorizedAccessException] {
            return [pscustomobject]@{ State = 'AccessDenied'; Attributes = [IO.FileAttributes]::Normal }
        }
        catch {
            return [pscustomobject]@{ State = 'Unknown'; Attributes = [IO.FileAttributes]::Normal }
        }
    }

    function Get-NormalizedPath {
        param([Parameter(Mandatory = $true)][string]$Path)

        $fullPath = [IO.Path]::GetFullPath($Path)
        $pathRoot = [IO.Path]::GetPathRoot($fullPath)
        if ($fullPath.Length -gt $pathRoot.Length) {
            $fullPath = $fullPath.TrimEnd([char[]]@('\', '/'))
        }
        return $fullPath
    }

    function Test-DirectoryMetadata {
        param(
            [Parameter(Mandatory = $true)][string]$Label,
            [Parameter(Mandatory = $true)][string]$Path
        )

        $metadata = Get-PathMetadata -Path $Path
        if ($metadata.State -eq 'Missing') {
            Add-Result -Label $Label -Status 'FAIL' -Reason 'directory is missing'
            return $false
        }
        if ($metadata.State -eq 'AccessDenied') {
            Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'metadata access was denied'
            return $false
        }
        if ($metadata.State -ne 'Present') {
            Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'directory metadata could not be inspected'
            return $false
        }
        if (($metadata.Attributes -band [IO.FileAttributes]::Directory) -eq 0) {
            Add-Result -Label $Label -Status 'FAIL' -Reason 'path is not a directory'
            return $false
        }
        if (($metadata.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Add-Result -Label $Label -Status 'FAIL' -Reason 'directory is a reparse point'
            return $false
        }

        Add-Result -Label $Label -Status 'PASS' -Reason 'directory exists and is not a reparse point'
        return $true
    }

    function Test-PathComponents {
        param(
            [Parameter(Mandatory = $true)][string]$Label,
            [Parameter(Mandatory = $true)][string]$Path,
            [switch]$AllowMissingLeaf
        )

        try {
            $fullPath = Get-NormalizedPath -Path $Path
            $root = [IO.Path]::GetPathRoot($fullPath)
            $relative = $fullPath.Substring($root.Length)
            $segments = @($relative.Split([char[]]@('\', '/'), [StringSplitOptions]::RemoveEmptyEntries))
            $current = $root

            for ($index = 0; $index -lt $segments.Count; $index++) {
                $current = Join-Path $current $segments[$index]
                $metadata = Get-PathMetadata -Path $current
                $isLeaf = $index -eq ($segments.Count - 1)

                if ($metadata.State -eq 'Missing') {
                    if ($AllowMissingLeaf -and $isLeaf) {
                        Add-Result -Label $Label -Status 'PASS' -Reason 'existing path components have no reparse points; leaf is not provisioned'
                        return $true
                    }
                    else {
                        Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'one or more path components are missing'
                        return $false
                    }
                }
                if ($metadata.State -eq 'AccessDenied') {
                    Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'metadata access was denied'
                    return $false
                }
                if ($metadata.State -ne 'Present') {
                    Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'path component metadata could not be inspected'
                    return $false
                }
                if (($metadata.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'physical path resolution crosses a reparse point'
                    return $false
                }
                if (-not $isLeaf -and (($metadata.Attributes -band [IO.FileAttributes]::Directory) -eq 0)) {
                    Add-Result -Label $Label -Status 'FAIL' -Reason 'a path component is not a directory'
                    return $false
                }
            }

            Add-Result -Label $Label -Status 'PASS' -Reason 'all existing path components are non-reparse points'
            return $true
        }
        catch {
            Add-Result -Label $Label -Status 'NOT VERIFIED' -Reason 'path components could not be inspected safely'
            return $false
        }
    }

    try {
        [void](Test-DirectoryMetadata -Label 'secret-directory' -Path $secretDirectory)
        [void](Test-PathComponents -Label 'secret-path-components' -Path $secretDirectory)

        foreach ($secretFile in $secretFiles) {
            $metadata = Get-PathMetadata -Path $secretFile.Path
            if ($metadata.State -eq 'Missing') {
                Add-Result -Label $secretFile.Label -Status 'FAIL' -Reason 'file is missing'
            }
            elseif ($metadata.State -eq 'AccessDenied') {
                Add-Result -Label $secretFile.Label -Status 'NOT VERIFIED' -Reason 'metadata access was denied'
            }
            elseif ($metadata.State -ne 'Present') {
                Add-Result -Label $secretFile.Label -Status 'NOT VERIFIED' -Reason 'file metadata could not be inspected'
            }
            elseif (($metadata.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                Add-Result -Label $secretFile.Label -Status 'FAIL' -Reason 'path is not a regular file'
            }
            elseif (($metadata.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Add-Result -Label $secretFile.Label -Status 'FAIL' -Reason 'file is a reparse point'
            }
            else {
                Add-Result -Label $secretFile.Label -Status 'PASS' -Reason 'regular non-reparse file exists'
            }

            [void](Test-PathComponents -Label ($secretFile.Label + '-path-components') -Path $secretFile.Path)
        }

        if ($null -eq $backupDirectory) {
            Add-Result -Label 'backup-directory' -Status 'NOT VERIFIED' -Reason 'Local AppData path is unavailable'
            Add-Result -Label 'backup-directory-owner-and-dacl' -Status 'NOT VERIFIED' -Reason 'backup directory is unavailable for policy inspection'
            Add-Result -Label 'backup-path-components' -Status 'NOT VERIFIED' -Reason 'backup path is unavailable'
            Add-Result -Label 'backup-location-outside-repository' -Status 'NOT VERIFIED' -Reason 'backup path could not be resolved'
        }
        else {
            $backupDirectoryOk = Test-DirectoryMetadata -Label 'backup-directory' -Path $backupDirectory
            $backupComponentsOk = Test-PathComponents -Label 'backup-path-components' -Path $backupDirectory -AllowMissingLeaf

            if (-not $backupDirectoryOk) {
                Add-Result -Label 'backup-directory-owner-and-dacl' -Status 'NOT VERIFIED' -Reason 'backup directory is not available for policy inspection'
            }
            else {
                try {
                    $acl = Get-Acl -LiteralPath $backupDirectory -ErrorAction Stop
                    $currentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
                    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier])
                    $rules = @($acl.GetAccessRules(
                        $true,
                        $true,
                        [Security.Principal.SecurityIdentifier]
                    ))
                    $expectedInheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
                    $policyMatches = $acl.AreAccessRulesProtected -and
                        $null -ne $ownerSid -and
                        $ownerSid.Value -ceq $currentUserSid.Value -and
                        $rules.Count -eq 1

                    if ($policyMatches) {
                        $rule = $rules[0]
                        $policyMatches =
                            $rule.IdentityReference.Value -ceq $currentUserSid.Value -and
                            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                            [int]$rule.FileSystemRights -eq [int][Security.AccessControl.FileSystemRights]::Modify -and
                            -not $rule.IsInherited -and
                            $rule.InheritanceFlags -eq $expectedInheritance -and
                            $rule.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None
                    }

                    if ($policyMatches) {
                        Add-Result -Label 'backup-directory-owner-and-dacl' -Status 'PASS' -Reason 'owner and exact restrictive DACL policy match'
                    }
                    else {
                        Add-Result -Label 'backup-directory-owner-and-dacl' -Status 'FAIL' -Reason 'owner or DACL does not match the defined policy'
                    }
                }
                catch {
                    Add-Result -Label 'backup-directory-owner-and-dacl' -Status 'NOT VERIFIED' -Reason 'owner or DACL could not be inspected'
                }
            }

            try {
                $repositoryCandidate = Join-Path $PSScriptRoot '..'
                $repositoryPath = (Resolve-Path -LiteralPath $repositoryCandidate -ErrorAction Stop).ProviderPath
                $repositoryComponentsOk = Test-PathComponents -Label 'repository-path-components' -Path $repositoryPath
                $repositoryFull = Get-NormalizedPath -Path $repositoryPath
                $backupFull = Get-NormalizedPath -Path $backupDirectory
                $repositoryPrefix = $repositoryFull + [IO.Path]::DirectorySeparatorChar
                $insideRepository =
                    $backupFull.Equals($repositoryFull, [StringComparison]::OrdinalIgnoreCase) -or
                    $backupFull.StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)

                if ($insideRepository) {
                    Add-Result -Label 'backup-location-outside-repository' -Status 'FAIL' -Reason 'normalized backup path is inside the repository'
                }
                elseif (-not $repositoryComponentsOk -or -not $backupComponentsOk) {
                    Add-Result -Label 'backup-location-outside-repository' -Status 'NOT VERIFIED' -Reason 'physical path boundary could not be established'
                }
                else {
                    Add-Result -Label 'backup-location-outside-repository' -Status 'PASS' -Reason 'lexical boundary check passed; inspected path components contained no reparse points'
                }
            }
            catch {
                Add-Result -Label 'backup-location-outside-repository' -Status 'NOT VERIFIED' -Reason 'repository boundary could not be established safely'
            }
        }

        Add-Result -Label 'backup-location-writable' -Status 'NOT VERIFIED' -Reason 'write access was not tested'
    }
    catch {
        Add-Result -Label 'preflight' -Status 'NOT VERIFIED' -Reason 'metadata inspection could not be completed safely'
    }

    foreach ($result in $results) {
        Write-Output $result
    }
}
