"""
Retroactively split an existing result folder into per-paper dashboards.
Usage:
    python _regen_per_paper.py data/result-20260423_133806 [output_prefix]
"""
import sys
import json
from pathlib import Path
import pandas as pd

sys.path.insert(0, '.')
from citationclaw.core.dashboard_generator import DashboardGenerator


def main(folder: Path, output_prefix: str = 'paper'):
    cfg = json.load(open('config.json', encoding='utf-8'))

    main_excel   = folder / f'{output_prefix}_results.xlsx'
    all_renowned = folder / f'{output_prefix}_results_all_renowned_scholar.xlsx'
    top_renowned = folder / f'{output_prefix}_results_top-tier_scholar.xlsx'
    citing_desc  = folder / f'{output_prefix}_results_with_citing_desc.xlsx'

    source = citing_desc if citing_desc.exists() else main_excel
    print(f'[source] {source}')

    df_main = pd.read_excel(source)
    df_all  = pd.read_excel(all_renowned) if all_renowned.exists() else None
    df_top  = pd.read_excel(top_renowned) if top_renowned.exists() else None

    if 'Citing_Paper' not in df_main.columns:
        print('ERROR: no Citing_Paper column in main excel')
        return 1

    canonical_titles = df_main['Citing_Paper'].dropna().astype(str).str.strip().unique().tolist()
    canonical_titles = [t for t in canonical_titles if t]
    print(f'Found {len(canonical_titles)} unique papers:')
    for t in canonical_titles:
        print(f'  - {t[:100]}')

    if len(canonical_titles) < 2:
        print('Only one paper found — nothing to split. Abort.')
        return 0

    per_paper_dir = folder / 'per_paper_reports'
    per_paper_dir.mkdir(parents=True, exist_ok=True)

    gen = DashboardGenerator(
        api_key=cfg.get('gemini_api_key') or cfg.get('openai_api_key'),
        base_url=cfg.get('openai_base_url'),
        model=cfg.get('dashboard_model') or cfg.get('openai_model'),
        log_callback=lambda m: print('   ', m),
        test_mode=cfg.get('test_mode', False),
    )

    def fwd(p): return str(p).replace('\\', '/')

    outputs = []
    for idx, canonical in enumerate(canonical_titles, start=1):
        print(f'\n=== [{idx}/{len(canonical_titles)}] {canonical[:80]} ===')
        target = canonical.strip()

        df_paper = df_main[
            df_main['Citing_Paper'].astype(str).str.strip() == target
        ].reset_index(drop=True)
        if df_paper.empty:
            print('  (empty — skip)')
            continue

        p_citing = per_paper_dir / f'{output_prefix}_paper{idx}_results_with_citing_desc.xlsx'
        df_paper.to_excel(p_citing, index=False)

        if df_all is not None and 'CitingPaper' in df_all.columns:
            p_all = per_paper_dir / f'{output_prefix}_paper{idx}_all_renowned_scholar.xlsx'
            df_all[df_all['CitingPaper'].astype(str).str.strip() == target].reset_index(drop=True).to_excel(p_all, index=False)
        else:
            p_all = all_renowned

        if df_top is not None and 'CitingPaper' in df_top.columns:
            p_top = per_paper_dir / f'{output_prefix}_paper{idx}_top-tier_scholar.xlsx'
            df_top[df_top['CitingPaper'].astype(str).str.strip() == target].reset_index(drop=True).to_excel(p_top, index=False)
        else:
            p_top = top_renowned

        output_html = folder / f'{output_prefix}_paper{idx}_dashboard.html'
        try:
            gen.generate(
                citing_desc_excel=p_citing,
                renowned_all_xlsx=p_all,
                renowned_top_xlsx=p_top,
                output_html=output_html,
                canonical_titles=[canonical],
                download_filenames={
                    'excel':        fwd(p_citing),
                    'all_renowned': fwd(p_all),
                    'top_renowned': fwd(p_top),
                },
                skip_citing_analysis=cfg.get('dashboard_skip_citing_analysis', True),
            )
            outputs.append(output_html)
            print(f'  ✅ generated: {output_html.name}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'  ❌ failed: {e}')

    print(f'\n=== DONE ===')
    print(f'Generated {len(outputs)} per-paper dashboards:')
    for p in outputs:
        print(f'  {p}')
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    folder = Path(sys.argv[1])
    prefix = sys.argv[2] if len(sys.argv) > 2 else 'paper'
    sys.exit(main(folder, prefix))
