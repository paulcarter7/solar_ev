import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3n from "aws-cdk-lib/aws-s3-notifications";
import { Construct } from "constructs";
import * as path from "path";

export class SolarEvStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // -------------------------------------------------------------------------
    // DynamoDB Tables
    // -------------------------------------------------------------------------

    // Stores hourly solar production readings and weather snapshots.
    // PK: deviceId (e.g. "enphase-system-123")  SK: timestamp (ISO-8601)
    const energyTable = new dynamodb.Table(this, "EnergyReadings", {
      tableName: "solar-ev-energy-readings",
      partitionKey: { name: "deviceId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: true,
      },
      timeToLiveAttribute: "ttl", // auto-expire readings older than 90 days
    });

    // Stores anomalies detected during ingest (low/no production, battery issues).
    // PK: systemId  SK: timestamp (ISO-8601 UTC). 30-day TTL.
    const anomalyTable = new dynamodb.Table(this, "AnomalyReadings", {
      tableName: "solar-ev-anomalies",
      partitionKey: { name: "systemId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      timeToLiveAttribute: "ttl",
    });

    // Stores user configuration: utility rates, EV specs, notification prefs.
    // PK: userId  SK: configType (e.g. "utility", "ev", "notifications")
    const configTable = new dynamodb.Table(this, "UserConfig", {
      tableName: "solar-ev-user-config",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "configType", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -------------------------------------------------------------------------
    // Shared Lambda environment variables
    // -------------------------------------------------------------------------
    const sharedEnv = {
      ENERGY_TABLE: energyTable.tableName,
      CONFIG_TABLE: configTable.tableName,
      ENPHASE_SYSTEM_ID: "6046451",
      LOG_LEVEL: "INFO",
    };

    // -------------------------------------------------------------------------
    // Lambda: solar_data — returns today's solar production (mock for now)
    // -------------------------------------------------------------------------
    const solarDataFn = new lambda.Function(this, "SolarDataFn", {
      functionName: "solar-ev-solar-data",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/solar_data")
      ),
      environment: sharedEnv,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Returns today's solar production data (real or mock)",
    });

    energyTable.grantReadWriteData(solarDataFn);
    configTable.grantReadData(solarDataFn);

    // -------------------------------------------------------------------------
    // Lambda: recommendation — computes best EV charging window
    // -------------------------------------------------------------------------
    const recommendationFn = new lambda.Function(this, "RecommendationFn", {
      functionName: "solar-ev-recommendation",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/recommendation")
      ),
      environment: sharedEnv,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Computes best EV charging window from solar + TOU rates",
    });

    energyTable.grantReadData(recommendationFn);
    configTable.grantReadData(recommendationFn);

    // -------------------------------------------------------------------------
    // Lambda: history — returns N days of daily production totals
    // -------------------------------------------------------------------------
    const historyFn = new lambda.Function(this, "HistoryFn", {
      functionName: "solar-ev-history",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/history")
      ),
      environment: sharedEnv,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Returns daily production totals for the requested date range",
    });

    energyTable.grantReadData(historyFn);

    // -------------------------------------------------------------------------
    // Lambda: ingest — runs on schedule to pull Enphase + weather data
    // -------------------------------------------------------------------------
    // SSM parameter paths for secrets — values stored via:
    //   aws ssm put-parameter --name /solar-ev/enphase-api-key \
    //     --value "YOUR_KEY" --type SecureString
    //
    // Curtailment alerts via ntfy.sh — store your topic name:
    //   aws ssm put-parameter --name /solar-ev/ntfy-topic \
    //     --value "your-secret-topic" --type SecureString
    //   Then subscribe in the ntfy app to that topic name.
    const SSM_PREFIX = "/solar-ev";

    const ingestFn = new lambda.Function(this, "IngestFn", {
      functionName: "solar-ev-ingest",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/ingest")
      ),
      environment: {
        ...sharedEnv,
        ANOMALY_TABLE: anomalyTable.tableName,
        // Non-sensitive config — safe as plain env vars
        ENPHASE_SYSTEM_ID: "6046451",
        LOCATION_LAT: "37.8216",
        LOCATION_LON: "-121.9999",
        // SSM paths — Lambda reads and decrypts these at runtime
        ENPHASE_API_KEY_PARAM:       `${SSM_PREFIX}/enphase-api-key`,
        ENPHASE_ACCESS_TOKEN_PARAM:  `${SSM_PREFIX}/enphase-access-token`,
        ENPHASE_REFRESH_TOKEN_PARAM: `${SSM_PREFIX}/enphase-refresh-token`,
        ENPHASE_CLIENT_ID_PARAM:     `${SSM_PREFIX}/enphase-client-id`,
        ENPHASE_CLIENT_SECRET_PARAM: `${SSM_PREFIX}/enphase-client-secret`,
        OPENWEATHER_API_KEY_PARAM:   `${SSM_PREFIX}/openweather-api-key`,
        NTFY_TOPIC_PARAM:            `${SSM_PREFIX}/ntfy-topic`,
      },
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Hourly ingest: pulls Enphase + OpenWeatherMap data",
    });

    energyTable.grantReadWriteData(ingestFn);
    anomalyTable.grantWriteData(ingestFn);
    // Ingest writes de-dup records to config table to prevent alert spam
    configTable.grantReadWriteData(ingestFn);

    // Allow ingest Lambda to read all /solar-ev/* params (covers ntfy-topic too)
    ingestFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${SSM_PREFIX}/*`,
        ],
      })
    );

    // Allow ingest Lambda to overwrite only the token params (needed for auto-refresh)
    ingestFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ssm:PutParameter"],
        resources: [
          `arn:aws:ssm:${this.region}:${this.account}:parameter${SSM_PREFIX}/enphase-access-token`,
          `arn:aws:ssm:${this.region}:${this.account}:parameter${SSM_PREFIX}/enphase-refresh-token`,
        ],
      })
    );

    // -------------------------------------------------------------------------
    // EventBridge rule — trigger ingest every hour
    // -------------------------------------------------------------------------
    new events.Rule(this, "HourlyIngestRule", {
      ruleName: "solar-ev-hourly-ingest",
      description: "Trigger solar/weather data ingest every hour",
      schedule: events.Schedule.rate(cdk.Duration.hours(1)),
      targets: [new targets.LambdaFunction(ingestFn)],
    });

    // -------------------------------------------------------------------------
    // S3: document storage for RAG ingestion
    // -------------------------------------------------------------------------
    const documentsBucket = new s3.Bucket(this, "DocumentsBucket", {
      bucketName: `solar-ev-documents-${this.account}-${this.region}`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
    });

    // -------------------------------------------------------------------------
    // Shared IAM policies for RAG Lambdas
    // -------------------------------------------------------------------------
    // Bedrock is unavailable in us-west-1 — Lambdas call it cross-region via us-east-1.
    // Newer Claude models require inference profiles (us.*) rather than direct model IDs.
    const BEDROCK_REGION = "us-east-1";
    const bedrockPolicy = new iam.PolicyStatement({
      actions: ["bedrock:InvokeModel"],
      resources: [
        // Titan embeddings (foundation model, no account ID)
        `arn:aws:bedrock:${BEDROCK_REGION}::foundation-model/amazon.titan-embed-text-v2:0`,
        // Nova Lite cross-region inference profile
        `arn:aws:bedrock:${BEDROCK_REGION}:${this.account}:inference-profile/us.amazon.nova-lite-v1:0`,
        `arn:aws:bedrock:${BEDROCK_REGION}::foundation-model/us.amazon.nova-lite-v1:0`,
        `arn:aws:bedrock:*::foundation-model/amazon.nova-lite-v1:0`,
      ],
    });

    const neonSsmPolicy = new iam.PolicyStatement({
      actions: ["ssm:GetParameter"],
      resources: [
        `arn:aws:ssm:${this.region}:${this.account}:parameter${SSM_PREFIX}/neon-connection-string`,
      ],
    });

    // pg8000 and pypdf are pure Python — no Docker needed for bundling.
    // Local bundling: pip installs deps, copies handler files, copies shared/neon.py.
    // Falls back to Docker if local pip isn't available (unlikely on a dev machine).
    const sharedDir = path.join(__dirname, "../../backend/shared");
    const bundleFn = (fnDir: string): cdk.BundlingOptions => ({
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      local: {
        tryBundle(outputDir: string): boolean {
          try {
            const { execSync } = require("child_process");
            execSync(
              `pip install -r "${path.join(fnDir, "requirements.txt")}" -t "${outputDir}" --quiet`,
              { stdio: ["ignore", "inherit", "inherit"] }
            );
            execSync(`cp -r "${fnDir}/." "${outputDir}/"`, { stdio: "inherit" });
            execSync(`cp "${path.join(sharedDir, "neon.py")}" "${outputDir}/"`, { stdio: "inherit" });
            return true;
          } catch (e) {
            return false;
          }
        },
      },
      command: [
        "bash",
        "-c",
        `pip install -r requirements.txt -t /asset-output --quiet && cp -au . /asset-output`,
      ],
    });

    const ragEnv = {
      NEON_CONNECTION_STRING_PARAM: `${SSM_PREFIX}/neon-connection-string`,
      BEDROCK_REGION: BEDROCK_REGION,
      BEDROCK_EMBEDDING_MODEL: "amazon.titan-embed-text-v2:0",
    };

    // -------------------------------------------------------------------------
    // Lambda: doc_ingest — S3 PDF → Bedrock Titan embeddings → Neon pgvector
    // -------------------------------------------------------------------------
    const docIngestDir = path.join(__dirname, "../../backend/functions/doc_ingest");
    const docIngestFn = new lambda.Function(this, "DocIngestFn", {
      functionName: "solar-ev-doc-ingest",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(docIngestDir, { bundling: bundleFn(docIngestDir) }),
      environment: {
        ...ragEnv,
        DOCUMENTS_BUCKET: documentsBucket.bucketName,
      },
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Ingests PDF docs from S3 into Neon pgvector via Bedrock embeddings",
    });

    documentsBucket.grantRead(docIngestFn);
    docIngestFn.addToRolePolicy(bedrockPolicy);
    docIngestFn.addToRolePolicy(neonSsmPolicy);

    // Trigger ingestion whenever a .pdf is uploaded
    documentsBucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(docIngestFn),
      { suffix: ".pdf" }
    );

    // -------------------------------------------------------------------------
    // Lambda: rag_query — query → pgvector similarity search → Bedrock Claude
    // -------------------------------------------------------------------------
    const ragQueryDir = path.join(__dirname, "../../backend/functions/rag_query");
    const ragQueryFn = new lambda.Function(this, "RagQueryFn", {
      functionName: "solar-ev-rag-query",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(ragQueryDir, { bundling: bundleFn(ragQueryDir) }),
      environment: {
        ...ragEnv,
        BEDROCK_GENERATION_MODEL: "us.amazon.nova-lite-v1:0",
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Answers natural language queries via RAG over ingested documents",
    });

    ragQueryFn.addToRolePolicy(bedrockPolicy);
    ragQueryFn.addToRolePolicy(neonSsmPolicy);

    // -------------------------------------------------------------------------
    // API Gateway
    // -------------------------------------------------------------------------
    const api = new apigateway.RestApi(this, "SolarEvApi", {
      restApiName: "solar-ev-api",
      description: "Home Energy Optimizer API",
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: apigateway.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
      deployOptions: {
        stageName: "prod",
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        dataTraceEnabled: false,
        metricsEnabled: true,
        throttlingBurstLimit: 100,
        throttlingRateLimit: 50,
      },
    });

    // GET /solar/today  and  GET /solar/history
    const solar = api.root.addResource("solar");
    const solarToday = solar.addResource("today");
    solarToday.addMethod(
      "GET",
      new apigateway.LambdaIntegration(solarDataFn, { proxy: true })
    );

    const solarHistory = solar.addResource("history");
    solarHistory.addMethod(
      "GET",
      new apigateway.LambdaIntegration(historyFn, { proxy: true })
    );

    // GET /recommendation
    const recommendation = api.root.addResource("recommendation");
    recommendation.addMethod(
      "GET",
      new apigateway.LambdaIntegration(recommendationFn, { proxy: true })
    );

    // -------------------------------------------------------------------------
    // Lambda: chat — classifies query and routes to rag_query or data_query
    // -------------------------------------------------------------------------
    const chatFn = new lambda.Function(this, "ChatFn", {
      functionName: "solar-ev-chat",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/chat")
      ),
      environment: {
        BEDROCK_REGION: BEDROCK_REGION,
        BEDROCK_GENERATION_MODEL: "us.amazon.nova-lite-v1:0",
        RAG_QUERY_FUNCTION_NAME: ragQueryFn.functionName,
        DATA_QUERY_FUNCTION_NAME: dataQueryFn.functionName,
        ANOMALY_QUERY_FUNCTION_NAME: anomalyQueryFn.functionName,
      },
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Routes chat queries to rag_query (documents) or data_query (DynamoDB)",
    });

    chatFn.addToRolePolicy(bedrockPolicy);
    ragQueryFn.grantInvoke(chatFn);
    dataQueryFn.grantInvoke(chatFn);
    anomalyQueryFn.grantInvoke(chatFn);

    // POST /chat
    const chatResource = api.root.addResource("chat");
    chatResource.addMethod(
      "POST",
      new apigateway.LambdaIntegration(chatFn, { proxy: true })
    );

    // -------------------------------------------------------------------------
    // Lambda: data_query — natural language → DynamoDB query → formatted answer
    // -------------------------------------------------------------------------
    const dataQueryFn = new lambda.Function(this, "DataQueryFn", {
      functionName: "solar-ev-data-query",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/data_query")
      ),
      environment: {
        ...sharedEnv,
        BEDROCK_REGION: BEDROCK_REGION,
        BEDROCK_GENERATION_MODEL: "us.amazon.nova-lite-v1:0",
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Answers natural language questions about energy data in DynamoDB",
    });

    energyTable.grantReadData(dataQueryFn);
    dataQueryFn.addToRolePolicy(bedrockPolicy);

    // POST /data-query
    const dataQuery = api.root.addResource("data-query");
    dataQuery.addMethod(
      "POST",
      new apigateway.LambdaIntegration(dataQueryFn, { proxy: true })
    );

    // -------------------------------------------------------------------------
    // Lambda: anomaly_query — summarises detected anomalies with Nova Lite
    // -------------------------------------------------------------------------
    const anomalyQueryFn = new lambda.Function(this, "AnomalyQueryFn", {
      functionName: "solar-ev-anomaly-query",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "handler.lambda_handler",
      code: lambda.Code.fromAsset(
        path.join(__dirname, "../../backend/functions/anomaly_query")
      ),
      environment: {
        ...sharedEnv,
        ANOMALY_TABLE: anomalyTable.tableName,
        BEDROCK_REGION: BEDROCK_REGION,
        BEDROCK_GENERATION_MODEL: "us.amazon.nova-lite-v1:0",
      },
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Queries anomaly table and summarises issues with Nova Lite",
    });

    anomalyTable.grantReadData(anomalyQueryFn);
    anomalyQueryFn.addToRolePolicy(bedrockPolicy);

    // POST /anomalies
    const anomalies = api.root.addResource("anomalies");
    anomalies.addMethod(
      "POST",
      new apigateway.LambdaIntegration(anomalyQueryFn, { proxy: true })
    );

    // -------------------------------------------------------------------------
    // Outputs
    // -------------------------------------------------------------------------
    new cdk.CfnOutput(this, "ApiUrl", {
      value: api.url,
      description: "API Gateway base URL",
      exportName: "SolarEvApiUrl",
    });

    new cdk.CfnOutput(this, "EnergyTableName", {
      value: energyTable.tableName,
      description: "DynamoDB energy readings table",
    });

    new cdk.CfnOutput(this, "ConfigTableName", {
      value: configTable.tableName,
      description: "DynamoDB user config table",
    });

    new cdk.CfnOutput(this, "DocumentsBucketName", {
      value: documentsBucket.bucketName,
      description: "S3 bucket for RAG document ingestion — upload PDFs here",
    });
  }
}
