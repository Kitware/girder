import { PlaywrightTestConfig } from "@playwright/test";
import libCoverage from 'istanbul-lib-coverage';
import libReport from 'istanbul-lib-report';
import reports from 'istanbul-reports';
import fs from 'fs/promises';

export default async (_config: PlaywrightTestConfig) => {
  console.log("Teardown");

  // Merge all the coverage data files into one coverage map
  const coverageMap = libCoverage.createCoverageMap({});
  const files = await fs.readdir('coverage/data');
  console.log(files);
  for (const file of files) {
    console.log(file);
    coverageMap.merge(JSON.parse((await fs.readFile(`coverage/data/${file}`)).toString()));
  }

  const context = libReport.createContext({
    dir: 'coverage/report',
    defaultSummarizer: 'nested',
    watermarks: {
      statements: [50, 80] as [number, number],
      functions: [50, 80] as [number, number],
      branches: [50, 80] as [number, number],
      lines: [50, 80] as [number, number],
    },
    coverageMap,
  })

  const report = reports.create('html', {
    skipEmpty: false,
  });

  report.execute(context);
};
