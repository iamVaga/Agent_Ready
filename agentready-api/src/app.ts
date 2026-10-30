import { redact, installPrivacyLogging } from './utils/privacy';
import express, { Application } from 'express';
import cors from 'cors';
import helmet from 'helmet';
import { config } from './config';
import routes from './routes';
import { errorHandler, notFoundHandler } from './middleware/errorHandler';

export function createApp(): Application {
  installPrivacyLogging();
  const app = express();

  const allowedOrigins = new Set([
    ...config.frontendUrls,
    'http://localhost:3000',
    'http://localhost:5173',
  ]);

  // Render (and most PaaS hosts) sit behind a proxy; without this the rate
  // limiter would see one shared IP and throttle everyone together.
  app.set('trust proxy', 1);

  // The report route serves HTML we build ourselves, including <pre> blocks of
  // user-site content. helmet's defaults are kept, but the report is explicitly
  // sandboxed by a strict CSP since it echoes strings from scanned sites.
  app.use(
    helmet({
      contentSecurityPolicy: {
        directives: {
          defaultSrc: ["'none'"],
          styleSrc: ["'unsafe-inline'"],
          imgSrc: ["'self'", 'data:'],
          baseUri: ["'none'"],
          formAction: ["'none'"],
          frameAncestors: ["'none'"],
        },
      },
      crossOriginResourcePolicy: { policy: 'cross-origin' },
    }),
  );

  app.use(
    cors({
      origin: (origin, callback) => {
        // Non-browser callers (curl, the sales-call demo script) send no Origin.
        if (!origin) return callback(null, true);
        if (allowedOrigins.has(origin)) return callback(null, true);

        try {
          const hostname = new URL(origin).hostname;
          if (/\.vercel\.app$/i.test(hostname) || /(^|\.)codenixlabs\.com$/i.test(hostname)) {
            return callback(null, true);
          }
        } catch {
          return callback(new Error('Invalid Origin'));
        }

        return callback(new Error('Not allowed by CORS'));
      },
      methods: ['GET', 'POST', 'OPTIONS'],
      allowedHeaders: ['Content-Type'],
    }),
  );

  app.use(express.json({ limit: '10kb' }));
  app.use(express.urlencoded({ extended: true, limit: '10kb' }));

  app.use((_req, res, next) => {
    const json = res.json.bind(res);
    res.json = (body: unknown) => json(redact(JSON.parse(JSON.stringify(body))));
    res.setHeader('Cache-Control', 'no-store');
    next();
  });
  app.use('/api', routes);

  app.get('/', (_req, res) => {
    res.json({
      success: true,
      message: 'AgentReady API — AI Store Readiness Checker',
      version: '1.0.0',
      health: '/api/health',
    });
  });

  app.use(notFoundHandler);
  app.use(errorHandler);

  return app;
}

export default createApp;
