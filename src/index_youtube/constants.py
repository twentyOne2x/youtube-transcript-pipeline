from src.utils import root_directory

AUTHORS = ['Kyle Samani', 'Threadguy']

FIRMS = ['kamino', 'meteora', 'bloxroute', 'sol', 'solana', 'helius', 'raydium', 'jito', 'orca', 'jupiter',
         'pump.fun', 'bonkbot', 'banana gun', 'axiom', 'photon', 'svm',
         'a16z','paradigm', 'anza']


# Note, the use of keywords List is an attempt at filtering YouTube videos by name content to reduce noise
KEYWORDS_TO_INCLUDE = ['perps', 'derivatives', 'options', 'order flow', 'orderflow', 'transaction', 'mev', 'ordering', 'dex', 'front-running', 'arbitrage', 'back-running',
            'maximal extractable value', 'trading games', 'timing games', 'arbitrage games', 'timing', 'on-chain games',
            'fees', 'defi', 'latency', 'market design', 'searcher', 'staking','market microstructure','telegram bot',
            'cow', 'liquidity', 'censorship', 'ofa', 'pfof', 'payment for order flow', 'decentralisation', 'decentralization', "incentive", "incentives", 'hyperliquid',
            'auction', 'mechanism design', 'Price-of-Anarchy', 'protocol economics', 'pools', 'firedancer'
           ]

KEYWORDS_TO_INCLUDE += AUTHORS
KEYWORDS_TO_INCLUDE += FIRMS

# , 'smart contract', 'eth global',  'evm',  #  'vitalik', 'buterin', bridge',
KEYWORDS_TO_EXCLUDE = ['sanction','howey','lawyer','joke', 'jokes', '#short', '#shorts', 'gensler', 'T-Shirt', "New year's breathing exercise",
                       'From lifespan to healthspan (1)', 'On promoting healthspan and quality of life (2)',
                       'Quick Bits', '#eth', 'Oslo Freedom Forum:', 'Why the SEC', 'SEC Commissioner', 'Oráculos', 'On promoting healthspan and quality of life',
                       'From lifespan to healthspan', 'On the decentralized web', 'Web3 Masterclass for JavaScript Developers',
                        'Preprofessional Course in Civil Engineering', 'Professional Courses in APAM',
                        'Professional Courses in Biomedical Engineering', 'Professional Courses in Chemical Engineering',
                        'Pre-Professional Course in Earth and Environmental Engineering', 'Tristan Naumann - Computer Science',
                       'The SEC favors cash over in-kind transactions when it comes to approving a spot Bitcoin ETF', 'Art and Awe in the Age of Machine Intelligence',
                       "The Builder-Hero's Journey", 'OgleCrypto tells the fascinating story of how he tracked down a group of DeFi hackers from Hong Kong',
                       "How DeFi Hack Negotiators Get the Job Done: The Chopping Block", "🧐 The proposed IRS reporting rules could adversely impact DeFi.",
                       'The new proposed IRS rules for reporting on crypto transactions are “unadministrable”', 'Oráculos', 'Web3 Masterclass for JavaScript Developers',
                       'The SEC favors cash over in-kind transactions when it comes to approving a spot Bitcoin ETF', "The Builder-Hero's Journey", 'OgleCrypto',
                       'How DeFi Hack Negotiators', 'IRS', 'LINK to Staking v0.2', 'Querying and Indexing Smart Contract Data on Ethereum', 'Recapitalizing the Degens',
                       'finance “shittier” than the ones in crypto.', 'Elisa Konofagou']

YOUTUBE_VIDEOS_CSV_FILE_PATH = f"{root_directory()}/data/links/youtube/youtube_videos.csv"
