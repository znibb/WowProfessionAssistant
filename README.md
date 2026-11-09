# WowProfessionAssistant

## Setup
### Linux
1. Create python venv: `python -m venv .venv`
1. Activate venv: `source .venv/bin/activate`
1. Install packages in venv: `pip install -r requirements.txt`
1. Install [Auctionator addon](https://www.curseforge.com/wow/addons/auctionator)
1. Symlink Auctionator.lua: `ln -s <WOW_DIR>/WTF/Accout/<ACCOUNT_NAME>/SavedVariables/Auctionator.lua ./prices/Auctionator.lua`

### Windows Powershell
1. Create python venv: `python -m venv .venv`
1. Activate venv: `.\.venv\Scripts\Activate.ps1`
1. Install packages in venv: `pip install -r requirements.txt`
1. Install [Auctionator addon](https://www.curseforge.com/wow/addons/auctionator)
1. Create Symlink:
```powershell
New-Item -ItemType Directory -Force .\prices
New-Item -ItemType SymbolicLink `
  -Path .\prices\Auctionator.lua `
  -Target '<PathToWoWInstallationFolder>_forever_\WTF\Account\<ACCOUNT_NAME>\SavedVariables\Auctionator.lua'
```

Verify the link with

```powershell
Get-Item .\prices\Auctionator.lua | Format-List FullName, LinkType, Target
```

## Usage
1. Run in-game Full Scan of the AH
1. Run `./update-prices.py auctionator` to port price data to JSON structure
1. Run `./profit-craft.py <profession|all>` to show what's profitable
1. Profit

Faction-specific recipes are marked with `(A)` for Alliance or `(H)` for Horde
after the recipe name, using `data/faction_recipes_manual.json`.
