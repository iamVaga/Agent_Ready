import { installPrivacyLogging } from './utils/privacy';
installPrivacyLogging();
import { createApp } from './app';
import { config } from './config';
import { connectDatabase } from './config/database';

async function start(): Promise<void> {
  // Connect before listening: an API that accepts scans it cannot save is worse
  // than one that is briefly unavailable.
  await connectDatabase();

  const app = createApp();
  app.listen(config.port, () => {
    console.log(`🚀 AgentReady API listening on http://localhost:${config.port} (${config.nodeEnv})`);
  });
}

start().catch((error) => {
  console.error('❌ Failed to start AgentReady API:', error);
  process.exit(1);
});
