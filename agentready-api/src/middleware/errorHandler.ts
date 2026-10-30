import { privacyLog, safeUrl, redactText } from '../utils/privacy';
import { NextFunction, Request, Response } from 'express';
import { InvalidUrlError } from '../utils/url';

export class HttpError extends Error {
  constructor(readonly statusCode: number, message: string) {
    super(message);
    this.name = 'HttpError';
  }
}

export function notFoundHandler(req: Request, res: Response): void {
  res.status(404).json({ success: false, error: `Route not found: ${req.method} ${safeUrl("https://local.invalid" + req.path, true)}` });
}

export function errorHandler(error: Error, _req: Request, res: Response, _next: NextFunction): void {
  if (error instanceof InvalidUrlError) {
    res.status(400).json({ success: false, error: redactText(error.message) });
    return;
  }

  if (error instanceof HttpError) {
    res.status(error.statusCode).json({ success: false, error: redactText(error.message) });
    return;
  }

  privacyLog('Unhandled error:', error);
  res.status(500).json({ success: false, error: 'Something went wrong on our side. Please try again.' });
}

/**
 * Wraps an async handler so a rejected promise reaches the error handler
 * instead of hanging the request — Express 4 does not do this itself.
 */
export function asyncHandler<T extends Request>(
  handler: (req: T, res: Response, next: NextFunction) => Promise<unknown>,
) {
  return (req: T, res: Response, next: NextFunction): void => {
    handler(req, res, next).catch(next);
  };
}
