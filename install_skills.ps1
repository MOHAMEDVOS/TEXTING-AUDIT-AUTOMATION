# All unique skill repos from skills.sh leaderboard
$repos = @(
    "vercel-labs/skills",
    "vercel-labs/agent-skills",
    "anthropics/skills",
    "soultrace-ai/soultrace-skill",
    "remotion-dev/skills",
    "microsoft/azure-skills",
    "vercel-labs/agent-browser",
    "microsoft/github-copilot-for-azure",
    "nextlevelbuilder/ui-ux-pro-max-skill",
    "obra/superpowers",
    "supabase/agent-skills",
    "shadcn/ui",
    "vercel-labs/next-skills",
    "roin-orca/skills",
    "coreyhaines31/marketingskills",
    "pbakaus/impeccable",
    "anthropics/claude-code",
    "squirrelscan/skills",
    "larksuite/cli",
    "better-auth/skills",
    "lllllllama/ai-paper-reproduction-skill",
    "firecrawl/cli",
    "google-labs-code/stitch-skills",
    "juliusbrussee/caveman",
    "wshobson/agents",
    "hugmouse/skills",
    "sleekdotdesign/agent-skills",
    "arvindrk/extract-design-system",
    "mattpocock/skills",
    "firebase/agent-skills",
    "get-convex/agent-skills",
    "currents-dev/playwright-best-practices-skill",
    "xixu-me/skills",
    "leonxlnx/taste-skill",
    "browser-use/browser-use",
    "charon-fan/agent-playbook",
    "neondatabase/agent-skills",
    "github/awesome-copilot",
    "emilkowalski/skill",
    "expo/skills",
    "pexoai/pexo-skills",
    "kepano/obsidian-skills",
    "hyf0/vue-skills",
    "sentry/dev",
    "googleworkspace/cli",
    "vercel/turborepo",
    "vercel/ai",
    "jimliu/baoyu-skills",
    "resciencelab/opc-skills",
    "microsoft/playwright-cli"
)

$total = $repos.Count
$current = 0
$failed = @()

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installing $total skill repos globally" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

foreach ($repo in $repos) {
    $current++
    Write-Host "[$current/$total] Installing: $repo" -ForegroundColor Yellow
    try {
        npx skills add $repo -g --all 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  WARNING: Non-zero exit for $repo" -ForegroundColor Red
            $failed += $repo
        } else {
            Write-Host "  OK" -ForegroundColor Green
        }
    } catch {
        Write-Host "  FAILED: $_" -ForegroundColor Red
        $failed += $repo
    }
    Write-Host ""
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  DONE! Installed $($total - $failed.Count)/$total repos" -ForegroundColor Green
if ($failed.Count -gt 0) {
    Write-Host "  Failed repos:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "    - $_" -ForegroundColor Red }
}
Write-Host "========================================" -ForegroundColor Cyan
