import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
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
    // Lambda: ingest — runs on schedule to pull Enphase + weather data
    // -------------------------------------------------------------------------
    // SSM parameter paths for secrets — values stored via:
    //   aws ssm put-parameter --name /solar-ev/enphase-api-key \
    //     --value "YOUR_KEY" --type SecureString
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
        // Non-sensitive config — safe as plain env vars
        ENPHASE_SYSTEM_ID: "6046451",
        LOCATION_LAT: "37.7749",
        LOCATION_LON: "-122.4194",
        // SSM paths — Lambda reads and decrypts these at runtime
        ENPHASE_API_KEY_PARAM:       `${SSM_PREFIX}/enphase-api-key`,
        ENPHASE_ACCESS_TOKEN_PARAM:  `${SSM_PREFIX}/enphase-access-token`,
        ENPHASE_REFRESH_TOKEN_PARAM: `${SSM_PREFIX}/enphase-refresh-token`,
        ENPHASE_CLIENT_ID_PARAM:     `${SSM_PREFIX}/enphase-client-id`,
        ENPHASE_CLIENT_SECRET_PARAM: `${SSM_PREFIX}/enphase-client-secret`,
        OPENWEATHER_API_KEY_PARAM:   `${SSM_PREFIX}/openweather-api-key`,
      },
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_MONTH,
      description: "Hourly ingest: pulls Enphase + OpenWeatherMap data",
    });

    energyTable.grantReadWriteData(ingestFn);

    // Allow ingest Lambda to read all /solar-ev/* params
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

    // GET /solar/today
    const solar = api.root.addResource("solar");
    const solarToday = solar.addResource("today");
    solarToday.addMethod(
      "GET",
      new apigateway.LambdaIntegration(solarDataFn, { proxy: true })
    );

    // GET /recommendation
    const recommendation = api.root.addResource("recommendation");
    recommendation.addMethod(
      "GET",
      new apigateway.LambdaIntegration(recommendationFn, { proxy: true })
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
  }
}
