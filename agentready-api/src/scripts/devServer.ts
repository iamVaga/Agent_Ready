import { installPrivacyLogging } from '../utils/privacy';
installPrivacyLogging();
/**
 * Development server with a throwaway database.
 *
 *   npm run dev:memory
 *
 * Identical to `npm run dev` except that when MONGODB_URI is not set it starts
 * an in-memory MongoDB instead of refusing to boot. That makes the whole flow —
 * scan, lead capture, comparison — runnable on a laptop with nothing installed,
 * which is what a demo needs.
 *
 * Deliberately a separate entry point from src/server.ts: production must keep
 * failing loudly when its database is missing, rather than quietly starting on
 * storage that disappears when the process exits.
 */
import mongoose from 'mongoose';
import { createApp } from '../app';
import { config } from '../config';

async function start(): Promise<void> {
  let uri = config.mongoUri;
  let stop: () => Promise<void> = async () => undefined;

  if (uri) {
    console.log('🔗 Using MONGODB_URI from the environment.');
  } else {
    console.log('🧪 No MONGODB_URI set — starting a temporary in-memory MongoDB.');
    console.log('   Scans and leads will be lost when this process stops.');
    const { MongoMemoryServer } = await import('mongodb-memory-server');
    const server = await MongoMemoryServer.create();
    uri = server.getUri();
    stop = async () => {
      await server.stop();
    };
  }

  await mongoose.connect(uri);
  console.log('✅ MongoDB connected');

  const app = createApp();
  const httpServer = app.listen(config.port, () => {
    console.log(`🚀 AgentReady API listening on http://localhost:${config.port}`);
    if (!config.email.apiKey) {
      console.log('✉️  RESEND_API_KEY not set — leads will still be saved, but no report emails will send.');
    }
  });

  const shutdown = async (): Promise<void> => {
    httpServer.close();
    await mongoose.disconnect();
    await stop();
    process.exit(0);
  };

  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

start().catch((error) => {
  console.error('❌ Failed to start the development server:', error);
  process.exit(1);
});
