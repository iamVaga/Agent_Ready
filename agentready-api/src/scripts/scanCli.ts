import { installPrivacyLogging } from '../utils/privacy';
installPrivacyLogging();
/**
 * Live scan from the command line, with no database and no server.
 *
 * This is the spot-check tool the spec's "done" criteria call for: point it at
 * real stores and read the output.
 *
 *   npm run scan -- https://example.com [https://another.com ...]
 *   npm run scan -- https://example.com --verbose
 *   npm run scan -- https://example.com --html ./report.html
 *
 * `--html` writes the same report a lead would be emailed, which is the quickest
 * way to eyeball the wording before showing it to someone.
 */
import fs from 'fs';
import path from 'path';
import { runScan } from '../services/scanEngine';
import { weightsFor } from '../services/scoring';
import { buildReportHtml } from '../services/reportHtml';
import { ScanDoc } from '../models/Scan';

const STATUS_ICON: Record<string, string> = {
  pass: '✅',
  warning: '⚠️ ',
  fail: '❌',
  skipped: '➖',
};

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const verbose = args.includes('--verbose');

  const htmlFlag = args.indexOf('--html');
  const htmlPath = htmlFlag !== -1 ? args[htmlFlag + 1] : null;
  // Only skip the value slot when --html is actually present; `htmlFlag + 1`
  // is 0 otherwise, which would swallow the first URL.
  const htmlValueIndex = htmlFlag === -1 ? -1 : htmlFlag + 1;

  const urls = args.filter((arg, index) => !arg.startsWith('-') && index !== htmlValueIndex);

  if (!urls.length) {
    console.error('Usage: npm run scan -- <url> [url ...] [--verbose] [--html <file>]');
    process.exit(1);
  }

  for (const url of urls) {
    console.log(`\n${'='.repeat(78)}\n🔍 ${url}\n${'='.repeat(78)}`);

    try {
      const result = await runScan(url);

      console.log(`\nGrade ${result.overallGrade}  (${result.overallScore}/100)  in ${(result.scanDurationMs / 1000).toFixed(1)}s`);
      console.log(`${result.summary}\n`);
      console.log(`Judged as: ${result.siteType} (${result.siteTypeConfidence} confidence)`);
      for (const reason of result.siteTypeEvidence) console.log(`  · ${reason}`);
      console.log(`\nPages scanned (${result.pagesScanned.length}):`);
      for (const page of result.pagesScanned) console.log(`  · ${page}`);
      if (result.jsRenderWarning) console.log('\n⚠️  JS-render warning: raw HTML is nearly empty.');
      if (result.partial) console.log('⚠️  Partial scan: some checks could not be completed.');

      const weights = weightsFor(result.siteType);
      console.log('');
      for (const check of result.checks) {
        const weight = weights[check.checkId];
        console.log(
          `${STATUS_ICON[check.status]} ${check.title.padEnd(40)} ${String(check.pointsAwarded).padStart(4)}/${String(
            check.pointsPossible,
          ).padEnd(3)} (weight ${weight})`,
        );
        console.log(`     ${check.details}`);
        if (verbose) {
          console.log(`\n     ${check.humanExplanation}\n`);
          if (check.generatedFix) {
            if (check.generatedFixTarget) console.log(`     Where this goes: ${check.generatedFixTarget}`);
            console.log('     --- generated fix ---');
            console.log(
              check.generatedFix
                .split('\n')
                .map((line) => `     ${line}`)
                .join('\n'),
            );
            console.log('     --- end fix ---\n');
          }
        }
      }
      if (htmlPath) {
        // buildReportHtml reads only the fields below; a plain object stands in
        // for the saved document so this works without a database.
        const asDoc = { ...result, _id: 'preview', scannedAt: new Date() } as unknown as ScanDoc;
        const target = path.resolve(htmlPath);
        fs.writeFileSync(target, buildReportHtml(asDoc, 'there'), 'utf8');
        console.log(`\n📄 Report written to ${target}`);
      }
    } catch (error) {
      console.error(`\n❌ ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
